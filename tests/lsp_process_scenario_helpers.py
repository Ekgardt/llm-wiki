"""Helpers shared by the LSP process concern test files split from test_lsp_process.py."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import compile_cache
import lsp_protocol
import pytest
import windows_workspace
from llm_wiki import (
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)

FAKE_SERVER = Path(__file__).with_name("fake_lsp_server.py").resolve()
OWNER_NONCE = "a" * 32
# A start that gives up at the startup wait ends near 10 s; the alternatives it
# must be told apart from are the child's 30 s sleep and four serialised waits
# (40 s). The old ceiling, the wait plus 0.75 s, measured the runner instead:
# 10.9 s on a loaded Windows runner (CI run 35860367016, 2026-09-23). See
# `docs/research/2026-09-23-a-barrier-proves-concurrency.md`.
_STARTUP_CEILING_SECONDS = lsp_process_limits.STARTUP_WAIT_SECONDS * 2

# A barrier waits for another thread to reach a point; a hold keeps a lock taken
# until the test says otherwise. Neither is the property under test, so both are
# generous — a loaded machine may take seconds to schedule a thread, and a hold
# that expires early would let a bounded assertion pass for the wrong reason.
_BARRIER_SECONDS = 30.0
_HOLD_SECONDS = 30.0

# The third kind of wait: the one that proves work completes *while* a lock is
# held instead of blocking on it. `_DEADLINE_SECONDS` is the deadline such a test
# hands the product; `_COMPLETES_WHILE_HELD_SECONDS` is the window it then watches
# in.
#
# The window is not the bound. What bounds the product is the deadline, and what
# proves the property is that the work finishes before the holder lets go. The
# window only has to be long enough to see that happen, so it must cover the
# deadline *plus everything the operating system charges around it*: starting the
# watcher thread, scheduling it back once the deadline expires, and unwinding two
# frames. On a loaded four-vCPU runner that overhead alone dwarfs a 50 ms
# deadline, which is why the bare 0.2 that used to stand here flickered on hosted
# macOS and reported nothing but `assert False is True`.
#
# The relationship is what keeps the window honest, so it is stated and checked
# here rather than left for the next reader to rediscover:
#
#   _DEADLINE_SECONDS < _COMPLETES_WHILE_HELD_SECONDS < _HOLD_SECONDS
#
# Above the deadline, or honouring the deadline could not be observed at all.
# Below the hold, so the lock is still held when the window closes — that is what
# makes a pass mean "it finished while the lock was held" rather than "the holder
# happened to let go first", and it is why widening the window does not weaken
# the assertion. Work that really blocked on the lock still fails.
_DEADLINE_SECONDS = 0.05
_COMPLETES_WHILE_HELD_SECONDS = 5.0
assert _DEADLINE_SECONDS < _COMPLETES_WHILE_HELD_SECONDS < _HOLD_SECONDS


def _lock_is_free(lock: object) -> bool:
    """Report whether nobody holds the lock, without waiting for it."""
    if not lock.acquire(blocking=False):  # type: ignore[attr-defined]
        return False
    lock.release()  # type: ignore[attr-defined]
    return True


def _cleanup_error_steps(coordinator) -> set[tuple[str, str]]:
    return {(item.step, item.error_type) for item in coordinator.cleanup_result.errors}


def _remove_temporaries(owner_root: Path, pattern: str) -> None:
    for temporary in owner_root.glob(pattern):
        temporary.unlink()


def _all_children_reaped(children) -> bool:
    return all(child.poll() is not None for child in children)


def _cleanup_error_types(coordinator) -> set[str]:
    return {item.error_type for item in coordinator.cleanup_result.errors}


def _owner_entry_names(process) -> set[str]:
    return {path.name for path in process.owner_root.iterdir()}


def _close_if_owned(process, coordinator) -> None:
    if lsp_process_state.coordinator_has_ownership(coordinator):
        process.close(time.monotonic() + 5)


def _close_owned_process(process, coordinator) -> None:
    """Close while ownership remains, retrying once past a transient refusal."""
    if not lsp_process_state.coordinator_has_ownership(coordinator):
        return
    try:
        process.close(time.monotonic() + 5)
    except RuntimeError:
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def _await_descendant_pid(pid_file: Path, timeout: float = 60.0) -> int:
    """Wait for the child to finish writing its PID, not merely to create the file.

    On Windows the child still holds the file while writing it, so an eager
    read raises PermissionError and a truncated read raises ValueError.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pid_file.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.01)
    raise AssertionError(f"descendant never recorded a PID in {pid_file}")


def _await_pid_exit(pid: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)


def _command(*arguments: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *arguments]


# Starting a server spawns a child, completes a handshake, publishes a lease
# and starts two workers. A few seconds is comfortable on a quiet machine and
# not enough on a loaded hosted runner, so the budget is sized for the slowest
# supported machine. Every test that wants a timeout injects one instead.
_STARTUP_BUDGET_SECONDS = 120


def _start(tmp_path: Path, *arguments: str) -> LspProcess:
    return LspProcess.start(
        _command(*arguments), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )


def _initialize_generation(
    protocol: lsp_protocol.LspProtocol,
    _pid: int,
    _generation_nonce: str,
    deadline: float,
) -> ProcessState:
    assert protocol.request("initialize", {}, deadline=deadline) == {"capabilities": {}}
    protocol.notify("initialized", {}, deadline=deadline)
    return ProcessState.PROTOCOL_INITIALIZED


def _wait(process: LspProcess, seconds: float = 10) -> int:
    return process.wait_for_exit(time.monotonic() + seconds)


def _expect_active_generation_exit(process: LspProcess) -> None:
    generation = process._coordinator.active
    assert generation is not None
    assert lsp_process_state.mark_generation_expected_exit(generation)


class _FakeTree:
    def __init__(self, process: object) -> None:
        self.process = process

    def terminate(self, *, deadline: float) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=max(0.0, (deadline - time.monotonic()) / 2))
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("fake tree stayed alive") from exc

    def close(self) -> None:
        if self.process.poll() is None:
            raise RuntimeError("fake tree stayed live")


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _posix_pid_alive(pid: int) -> bool:
    """EPERM means the process exists under another user, not that it is gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def _windows_handle_status(handle: int) -> tuple[bool, int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    ctypes.set_last_error(0)
    valid = bool(kernel32.GetHandleInformation(handle, ctypes.byref(flags)))
    return valid, ctypes.get_last_error()


def _assert_lsp_acl_is_owner_only(path: Path, *, inherited: bool) -> None:
    if os.name != "nt":
        compile_cache.verify_owner_only(path, 0o700 if path.is_dir() else 0o600)
        return
    acl = compile_cache.acl_output_text(
        compile_cache.run_acl_command(["icacls", str(path)]).stdout
    )
    acl_lines = [line.strip() for line in acl.splitlines() if ":(" in line]
    assert len(acl_lines) == 1
    assert compile_cache.acl_principal(path, acl_lines[0]).casefold() == (
        compile_cache.windows_acl_identity().casefold()
    )
    assert "(F)" in acl_lines[0]
    assert ("(I)" in acl_lines[0]) is inherited


def _poll_until(predicate, timeout: float = 5.0) -> bool:
    """Wait for state the transition lock holder must not block on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _coordinator_wait(process: LspProcess, predicate: object, timeout: float = 3.0) -> bool:
    coordinator = process._coordinator
    with coordinator.condition:
        return coordinator.condition.wait_for(predicate, timeout=timeout)  # type: ignore[arg-type]


def _mock_windows_artifact_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    close_handle: object,
    create_calls: list[str] | None = None,
) -> lsp_process_owner.OwnerDirectory:
    owner = lsp_process_owner.OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=(1, b"parent", True),
        owner_handle=202,
        owner_identity=(1, b"owner", True),
        owner_permissions_verified=True,
    )
    identity = (1, b"artifact", False)
    def create_file(_parent: int, name: str) -> int:
        if create_calls is not None:
            create_calls.append(name)
        return 303

    monkeypatch.setattr(windows_workspace, "create_file", create_file)
    monkeypatch.setattr(windows_workspace, "identity", lambda _handle, directory: identity)
    monkeypatch.setattr(windows_workspace, "write_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(windows_workspace, "flush_file", lambda _handle: None)
    monkeypatch.setattr(windows_workspace, "flush_directory", lambda _handle: True)
    monkeypatch.setattr(windows_workspace, "publish_file", lambda *_args: None)
    monkeypatch.setattr(windows_workspace, "replace_file", lambda *_args: None)
    monkeypatch.setattr(
        windows_workspace,
        "open_deletable_file",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )
    monkeypatch.setattr(windows_workspace, "delete_handle", lambda _handle: None)
    monkeypatch.setattr(windows_workspace, "close_handle", close_handle)
    return owner
