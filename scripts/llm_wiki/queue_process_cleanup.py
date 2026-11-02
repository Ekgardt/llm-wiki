"""Queue child-tree termination and verified cleanup; launch stays with the caller."""
from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from llm_wiki.queue_contracts import QueueOperationError


def _pid_is_alive(pid: int) -> bool:
    try:
        from process_liveness import pid_alive

        # Preserve this adapter's legacy invalid-PID policy; the common probe
        # itself reports invalid input as unknown.
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        return pid_alive(pid)
    except Exception:  # noqa: BLE001 - inability to prove death is treated as live
        return True


def _kill_process_group(pid: int, sig: int) -> None:
    os.killpg(pid, sig)


def _process_snapshot_posix() -> list[tuple[int, int, int, str]] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    rows: list[tuple[int, int, int, str]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2 :].split()
            rows.append((int(entry.name), int(fields[1]), int(fields[2]), fields[0]))
        except (OSError, ValueError, IndexError):
            continue
    return rows


_TH32CS_SNAPPROCESS = 0x2
# PROCESSENTRY32.szExeFile is CHAR[MAX_PATH] and MAX_PATH is 260 (Win32 tlhelp32.h); the ctypes
# layout must match it. External contract.
_MAX_PROCESS_PATH = 260


def _process_entry_type():
    """The PROCESSENTRY32 layout, built only when ctypes has Windows types."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            # ULONG_PTR: a pointer keeps the right width on 32- and 64-bit.
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * _MAX_PROCESS_PATH),
        ]

    return ProcessEntry32


def _process_snapshot_api():
    """Bind pointer-sized handles before calling the Windows snapshot API."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _configure_process_walk(kernel32, entry_type) -> None:
    import ctypes
    from ctypes import wintypes

    for name in ("Process32First", "Process32Next"):
        function = getattr(kernel32, name)
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(entry_type))
        function.restype = wintypes.BOOL


def _walk_process_snapshot(kernel32, snapshot) -> list[tuple[int, int]] | None:
    import ctypes

    entry = _process_entry_type()()
    _configure_process_walk(kernel32, type(entry))
    entry.dwSize = ctypes.sizeof(entry)
    pairs: list[tuple[int, int]] = []
    ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
    while ok:
        pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
        ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    # Win32 ERROR_NO_MORE_FILES (18) is the documented successful end.
    # Any other failure leaves an unknown tree, even after some valid rows.
    if ctypes.get_last_error() != 18:
        return None
    return pairs


def _windows_process_pairs() -> list[tuple[int, int]] | None:
    """Every (pid, parent pid) from the kernel32 process snapshot.

    The only source: it is present on every supported Windows, needs no
    subprocess, and answers in milliseconds. None means the snapshot failed,
    which callers read as an unknown tree rather than a clean one.
    """
    try:
        import ctypes

        kernel32 = _process_snapshot_api()
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, -1, ctypes.c_void_p(-1).value):
            return None
        try:
            return _walk_process_snapshot(kernel32, snapshot)
        finally:
            kernel32.CloseHandle(snapshot)
    except (AttributeError, OSError, ValueError):
        return None


def _descendants_of(root_pid: int, pairs: list[tuple[int, int]]) -> set[int]:
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid for pid, ppid in pairs if ppid in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def _posix_process_pairs() -> list[tuple[int, int]]:
    snapshot = _process_snapshot_posix()
    if snapshot is None:
        return []
    return [(pid, ppid) for pid, ppid, _pgrp, _state in snapshot]


def _tracked_descendant_pids(root_pid: int, platform_name: str) -> set[int] | None:
    if platform_name != "nt":
        return _descendants_of(root_pid, _posix_process_pairs())
    pairs = _windows_process_pairs()
    if pairs is None:
        return None
    return _descendants_of(root_pid, pairs)


def _process_group_alive_by_signal(group_id: int) -> bool:
    """Whether signal 0 says the group still exists."""
    try:
        _kill_process_group(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_group_alive(group_id: int) -> bool:
    snapshot = _process_snapshot_posix()
    if snapshot is None:
        return _process_group_alive_by_signal(group_id)
    return any(
        pgrp == group_id and state != "Z" for _pid, _ppid, pgrp, state in snapshot
    )


def _descendants_alive_in_snapshot(
    snapshot: Sequence[tuple[int, int, int, str]], descendants: set[int]
) -> bool:
    """Whether any tracked descendant is more than a zombie in this snapshot."""
    states = {pid: state for pid, _ppid, _pgrp, state in snapshot}
    return any(pid in states and states[pid] != "Z" for pid in descendants)


def _descendant_alive(descendants: set[int], *, platform_name: str) -> bool:
    """Whether any tracked descendant of the worker is still running."""
    snapshot = _process_snapshot_posix() if platform_name != "nt" else None
    if snapshot is None:
        return any(_pid_is_alive(pid) for pid in descendants)
    return _descendants_alive_in_snapshot(snapshot, descendants)


def _cleanup_confirmed(
    process: multiprocessing.Process,
    descendants: set[int],
    *,
    platform_name: str,
) -> bool:
    if process.is_alive():
        return False
    if _descendant_alive(descendants, platform_name=platform_name):
        return False
    return platform_name == "nt" or not _process_group_alive(process.pid)


_DISCOVER_DESCENDANTS = object()


def _require_windows_child_alive(
    process: multiprocessing.Process,
    platform_name: str,
    discover_descendants: bool,
    direct_was_alive: bool,
) -> None:
    """Windows cannot enumerate the descendants of a process that is already gone."""
    if direct_was_alive or platform_name != "nt" or not discover_descendants:
        return
    raise QueueOperationError(
        "process_cleanup_failed",
        f"child exited {getattr(process, 'exitcode', None)} before cleanup",
    )


def _discovered_descendants(
    process: multiprocessing.Process,
    platform_name: str,
    discover: bool,
    tracked: set[int] | None | object,
):
    if not discover:
        return tracked
    return _tracked_descendant_pids(process.pid, platform_name)


def _taskkill(pid: int) -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _kill_windows_tree(
    process: multiprocessing.Process, descendants: set[int] | None
) -> bool:
    tree_verified = _taskkill(process.pid)
    if descendants is None:
        return tree_verified
    for pid in descendants:
        if _pid_is_alive(pid):
            _taskkill(pid)
    return tree_verified


def _kill_posix_group(
    process: multiprocessing.Process, descendants: set[int] | None
) -> bool:
    try:
        _kill_process_group(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return os.name != "nt" and descendants == set()
    except OSError:
        return False
    return True


def _kill_process_tree(
    process: multiprocessing.Process,
    descendants: set[int] | None,
    platform_name: str,
) -> bool:
    if platform_name == "nt":
        return _kill_windows_tree(process, descendants)
    return _kill_posix_group(process, descendants)


def _hard_kill_group(
    process: multiprocessing.Process, platform_name: str, tree_verified: bool
) -> bool:
    """Follow the group's SIGTERM with SIGKILL, and judge what came back.

    A `ProcessLookupError` here is the outcome this call was asking for: the
    group is already gone, killed by the SIGTERM that preceded it. Recording
    that as an unverified tree is what turned a clean timeout into
    `process_cleanup_failed`, because `_cleanup_failed` refuses on POSIX
    whenever the tree is unverified — even when `_await_cleanup` has already
    confirmed the child and every descendant are gone.

    Traced on the live machine 2026-08-29 at load 9-11: the first attempt
    reported `kill_group=True`, `await_cleanup=True` and still
    `cleanup_failed=True` with `tree_verified=False`, which is only reachable
    through this branch. On a quiet machine the group is still there when the
    SIGKILL lands, the exception never fires, and the test passes — which is
    exactly how `test_worker_timeout_kills_spawned_grandchild_tree` behaved.

    Any other `OSError` — `EPERM` and its kin — still fails closed.
    """
    if not process.is_alive() or platform_name == "nt" or not tree_verified:
        return tree_verified
    verified = _sigkill_group_verified(process.pid)
    process.join(0.2)
    return verified


def _sigkill_group_verified(pid: int) -> bool:
    """Whether the group is gone after SIGKILL. Already gone counts as gone."""
    try:
        _kill_process_group(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _stop_quietly(process: multiprocessing.Process, action) -> None:
    if not process.is_alive():
        return
    try:
        action()
    except (OSError, ValueError):
        pass
    process.join(0.2)


def _escalate_termination(
    process: multiprocessing.Process, platform_name: str, tree_verified: bool
) -> bool:
    process.join(0.2)
    tree_verified = _hard_kill_group(process, platform_name, tree_verified)
    _stop_quietly(process, process.terminate)
    _stop_quietly(process, process.kill)
    return tree_verified


def _await_cleanup(
    process: multiprocessing.Process,
    descendants: set[int] | None,
    platform_name: str,
    cleanup_timeout: float,
) -> bool:
    if descendants is None:
        return False
    deadline = time.monotonic() + max(0.0, cleanup_timeout)
    return _poll_cleanup(process, descendants, platform_name, deadline)


def _poll_cleanup(
    process: multiprocessing.Process,
    descendants: set[int],
    platform_name: str,
    deadline: float,
) -> bool:
    """Whether cleanup is confirmed before the monotonic `deadline`."""
    while True:
        if _cleanup_confirmed(process, descendants, platform_name=platform_name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _cleanup_failed(
    cleanup_verified: bool, platform_name: str, tree_verified: bool
) -> bool:
    if not cleanup_verified:
        return True
    return platform_name != "nt" and not tree_verified


def _terminate_processor_child(
    process: multiprocessing.Process,
    *,
    platform_name: str | None = None,
    # A terminated Windows tree can take a long time to disappear on a loaded
    # machine, and the worker then reports `process_cleanup_failed` for a
    # cleanup that was merely still in progress. One second was not enough,
    # and neither was fifteen on a hosted four-vCPU runner. The wait is sized
    # for the slowest supported machine and stays bounded; the happy path
    # returns as soon as the tree is gone.
    cleanup_timeout: float = 60.0,
    tracked_descendants: set[int] | None | object = _DISCOVER_DESCENDANTS,
) -> set[int]:
    platform_name = platform_name or os.name
    direct_was_alive = process.is_alive()
    discover_descendants = tracked_descendants is _DISCOVER_DESCENDANTS
    _require_windows_child_alive(
        process, platform_name, discover_descendants, direct_was_alive
    )
    descendants = _discovered_descendants(
        process, platform_name, discover_descendants, tracked_descendants
    )
    if descendants is not None and _cleanup_confirmed(
        process, descendants, platform_name=platform_name
    ):
        return descendants
    tree_verified = _kill_process_tree(process, descendants, platform_name)
    tree_verified = _escalate_termination(process, platform_name, tree_verified)
    cleanup_verified = _await_cleanup(
        process, descendants, platform_name, cleanup_timeout
    )
    if _cleanup_failed(cleanup_verified, platform_name, tree_verified):
        raise QueueOperationError(
            "process_cleanup_failed",
            f"snapshot_known={descendants is not None}; tree_verified={tree_verified}; "
            f"termination_verified={cleanup_verified}",
        )
    return descendants
