"""Platform-qualified ownership and bounded cleanup of one LSP process tree.

A Windows Job Object owns the assigned server tree. A POSIX process group owns
the pinned server and descendants only while they remain in that group; a hostile
descendant can call setsid(), and containing that escape is unsupported. The POSIX
path is therefore limited to the qualified Pyright profile in trusted repositories.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if os.name == "posix":
    import fcntl

_LINUX_PROC_SCAN_LIMIT = 131_072
_LINUX_PROC_STAT_LIMIT = 4096
_LINUX_DEAD_STATES = frozenset({b"Z", b"X", b"x"})
_MAX_PASS_FDS = 8

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _MAX_JOB_PROCESS_IDS = 4096
    _WINDOWS_PID_SETTLE_SECONDS = 0.05
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_NO_MORE_FILES = 18

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        )

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        )

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        )

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = (
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("this_period_total_user_time", ctypes.c_longlong),
            ("this_period_total_kernel_time", ctypes.c_longlong),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        )

    class _BasicProcessIdList(ctypes.Structure):
        _fields_ = (
            ("number_of_assigned_processes", wintypes.DWORD),
            ("number_of_process_ids_in_list", wintypes.DWORD),
            ("process_id_list", ctypes.c_size_t * 1),
        )

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", ctypes.c_long),
            ("delta_priority", ctypes.c_long),
            ("flags", wintypes.DWORD),
        )

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _KERNEL32.QueryInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
    _KERNEL32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _KERNEL32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    _KERNEL32.Thread32First.restype = wintypes.BOOL
    _KERNEL32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    _KERNEL32.Thread32Next.restype = wintypes.BOOL
    _KERNEL32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _KERNEL32.OpenThread.restype = wintypes.HANDLE
    _KERNEL32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _KERNEL32.ResumeThread.restype = wintypes.DWORD
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL


class _ProcessTreeSpawnError(RuntimeError):
    """Process creation failed while OS ownership still needs cleanup."""

    def __init__(
        self,
        tree: ProcessTree | None,
        errors: tuple[BaseException, ...],
        *,
        windows_job: int | None = None,
    ) -> None:
        super().__init__("LSP process-tree setup cleanup is incomplete")
        self.tree = tree
        self.windows_job = tree.windows_job if tree is not None else windows_job
        self.errors = errors


@dataclass(slots=True)
class ProcessTree:
    process: subprocess.Popen[bytes]
    windows_job: int | None
    process_group: int | None

    @classmethod
    def spawn(
        cls, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> ProcessTree:
        return cls.spawn_with_deadline(
            command,
            cwd=cwd,
            env=env,
            deadline=time.monotonic() + 2.0,
        )

    @classmethod
    def spawn_with_deadline(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        deadline: float,
        pass_fds: Sequence[int] = (),
    ) -> ProcessTree:
        """Spawn only while the caller's absolute operation deadline is live."""
        return cls._spawn_with_deadline(
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )

    @classmethod
    def _spawn_with_deadline(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        deadline: float,
        pass_fds: Sequence[int] = (),
    ) -> ProcessTree:
        deadline = _deadline(deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("LSP process-tree spawn deadline expired")
        inherited_descriptors = _validated_pass_fds(pass_fds)
        options = _spawn_options(cwd, env)
        if os.name == "posix":
            return cls._spawn_posix(command, options, inherited_descriptors)
        if os.name != "nt":
            raise RuntimeError("LSP process trees are unsupported on this platform")
        return cls._spawn_windows(command, options, deadline)

    @classmethod
    def _spawn_posix(
        cls,
        command: Sequence[str],
        options: dict[str, object],
        inherited_descriptors: tuple[int, ...],
    ) -> ProcessTree:
        if inherited_descriptors:
            options["pass_fds"] = inherited_descriptors
        process = subprocess.Popen(list(command), start_new_session=True, **options)
        return cls(process, None, process.pid)

    @classmethod
    def _spawn_windows(
        cls,
        command: Sequence[str],
        options: dict[str, object],
        deadline: float,
    ) -> ProcessTree:
        job = _create_windows_job()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(command), creationflags=_CREATE_SUSPENDED, **options
            )
            tree = cls(process, job, None)
            _assign_windows_process(job, process)
            _resume_windows_process(process.pid)
            return tree
        except BaseException as setup_error:
            if process is None:
                cls._raise_unstarted_windows_failure(job, setup_error)
            cls._raise_started_windows_failure(process, job, deadline, setup_error)

    @staticmethod
    def _raise_unstarted_windows_failure(job: int, setup_error: BaseException) -> None:
        """No child exists yet; only the job object may need closing."""
        cleanup_errors: list[BaseException] = []
        try:
            _close_windows_handle(job)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise _ProcessTreeSpawnError(
                None,
                tuple(cleanup_errors),
                windows_job=job,
            ) from setup_error
        raise setup_error

    @classmethod
    def _raise_started_windows_failure(
        cls,
        process: subprocess.Popen[bytes],
        job: int,
        deadline: float,
        setup_error: BaseException,
    ) -> None:
        """A child exists: tear the tree down before re-raising the setup error."""
        tree = cls(process, job, None)
        cleanup_errors: list[BaseException] = []
        _collect_cleanup_error(cleanup_errors, lambda: tree.terminate(deadline=deadline))
        _collect_cleanup_error(cleanup_errors, tree.close)
        child_reaped = _reaped_or_recorded(process, cleanup_errors)
        if tree.windows_job is not None or not child_reaped:
            raise _ProcessTreeSpawnError(tree, tuple(cleanup_errors)) from setup_error
        raise setup_error

    def has_live_descendants(self) -> bool:
        """Observe descendants after the direct process has been reaped."""
        if self.process.poll() is None:
            raise RuntimeError("direct LSP process is still live")
        if os.name == "nt":
            return _bounded_job_active_processes(self._owned_job()) != 0
        direct_reaped, group_absent = _observe_posix_tree(
            self.process, self._owned_group()
        )
        if not direct_reaped:
            raise RuntimeError("direct LSP process was not reaped")
        return not group_absent

    def _owned_job(self) -> int:
        job = self.windows_job
        if job is None:
            raise RuntimeError("Windows LSP process tree ownership was released")
        return job

    def _owned_group(self) -> int:
        group = self.process_group
        if group is None:
            raise RuntimeError("POSIX LSP process group ownership was released")
        return group

    def terminate(self, *, deadline: float) -> None:
        deadline = _deadline(deadline)
        if os.name == "nt":
            self._terminate_windows(deadline)
            return
        self._terminate_posix(deadline)

    def _terminate_posix(self, deadline: float) -> None:
        group = self._owned_group()
        errors: list[BaseException] = []
        started = time.monotonic()
        graceful_deadline = min(
            deadline,
            started + min(0.5, max(0.0, (deadline - started) / 2)),
        )
        _signal_group(group, signal.SIGTERM, errors)
        complete = _wait_posix_tree(self.process, group, graceful_deadline)
        if not complete:
            _signal_group(group, signal.SIGKILL, errors)
            complete = _wait_posix_tree(self.process, group, deadline)
        if errors:
            raise errors[0]
        if not complete:
            raise TimeoutError("LSP process group did not exit before deadline")

    def _terminate_windows(self, deadline: float) -> None:
        job = self._owned_job()
        errors: list[BaseException] = []
        snapshot_errors: list[BaseException] = []
        tracked_pids = _tracked_job_pids(job, snapshot_errors)
        _terminate_windows_job(job, errors)
        self._kill_direct_windows_process(deadline, errors)
        complete = self._await_windows_tree(job, deadline, tracked_pids, errors)
        if not complete:
            errors[:0] = snapshot_errors
        if errors:
            raise errors[0]
        if not complete:
            raise TimeoutError("LSP Windows process tree did not exit before deadline")

    def _kill_direct_windows_process(
        self, deadline: float, errors: list[BaseException]
    ) -> None:
        if not _reaped_or_recorded(self.process, errors):
            _collect_cleanup_error(errors, self.process.kill)
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return
        except BaseException as error:  # noqa: BLE001 - reported by the caller
            errors.append(error)

    def _await_windows_tree(
        self,
        job: int,
        deadline: float,
        tracked_pids: tuple[int, ...],
        errors: list[BaseException],
    ) -> bool:
        try:
            complete, observe_errors = _wait_windows_tree(
                self.process,
                job,
                deadline,
                tracked_pids=tracked_pids,
            )
        except BaseException as error:  # noqa: BLE001 - reported by the caller
            errors.append(error)
            return False
        errors.extend(observe_errors)
        return complete

    def close(self) -> None:
        if os.name == "nt":
            self._close_windows()
            return
        self._close_posix()

    def _close_windows(self) -> None:
        job = self.windows_job
        errors: list[BaseException] = []
        direct_reaped = _reaped_or_recorded(self.process, errors)
        active = _job_active_or_recorded(job, errors)
        if errors:
            raise errors[0]
        if not direct_reaped or active != 0:
            raise RuntimeError("Windows LSP process tree is still live")
        _close_windows_process_handle(self.process)
        if job is not None:
            _close_windows_handle(job)
            self.windows_job = None

    def _close_posix(self) -> None:
        group = self.process_group
        if group is None:
            return
        direct_reaped, group_absent = _observe_posix_tree(self.process, group)
        if not direct_reaped or not group_absent:
            raise RuntimeError("POSIX LSP process group is still live")
        self.process_group = None


def _tracked_job_pids(job: int, errors: list[BaseException]) -> tuple[int, ...]:
    try:
        return _job_process_ids(job)
    except BaseException as error:  # noqa: BLE001 - reported by the caller
        errors.append(error)
        return ()


def _terminate_windows_job(job: int, errors: list[BaseException]) -> None:
    try:
        if not _KERNEL32.TerminateJobObject(job, 1):
            errors.append(ctypes.WinError(ctypes.get_last_error()))
    except BaseException as error:  # noqa: BLE001 - reported by the caller
        errors.append(error)


def _job_active_or_recorded(job: int | None, errors: list[BaseException]) -> int | None:
    """Active process count, or None when the job could not be queried."""
    if job is None:
        return 0
    try:
        return _bounded_job_active_processes(job)
    except BaseException as error:  # noqa: BLE001 - reported by the caller
        errors.append(error)
        return None


@dataclass
class _WindowsWaitState:
    """Mutable observation state for one Windows tree wait."""

    remaining_pids: set[int]
    wait_error: BaseException | None = None
    query_failed: bool = False
    empty_observations: int = 0
    empty_since: float | None = None

    def reset_empty(self) -> None:
        self.empty_observations = 0
        self.empty_since = None

    def observe_empty(self, now: float) -> None:
        if self.empty_since is None:
            self.empty_since = now
        self.empty_observations += 1


def _signal_group(group: int, number: int, errors: list[BaseException]) -> None:
    """Signal the whole group; an already-gone group is not a failure."""
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        return
    except BaseException as error:  # noqa: BLE001 - reported by the caller
        errors.append(error)


def _pass_fds_sequence(pass_fds: Sequence[int]) -> tuple[int, ...]:
    if isinstance(pass_fds, (str, bytes)) or not isinstance(pass_fds, Sequence):
        raise TypeError("pass_fds must be a sequence of file descriptors")
    return tuple(pass_fds)


def _require_bounded_descriptor_set(descriptors: tuple[int, ...]) -> None:
    if any(not _is_descriptor(descriptor) for descriptor in descriptors):
        raise ValueError("pass_fds must contain nonnegative integers")
    if len(set(descriptors)) != len(descriptors):
        raise ValueError("pass_fds must not contain duplicates")
    if len(descriptors) > _MAX_PASS_FDS:
        raise ValueError(f"pass_fds must contain at most {_MAX_PASS_FDS} descriptors")


def _validated_pass_fds(pass_fds: Sequence[int]) -> tuple[int, ...]:
    """Read-only, unique, open descriptors — POSIX only, and bounded."""
    descriptors = _pass_fds_sequence(pass_fds)
    _require_bounded_descriptor_set(descriptors)
    if descriptors and os.name != "posix":
        raise ValueError("pass_fds are supported only on POSIX")
    _require_read_only_descriptors(descriptors)
    return descriptors


def _is_descriptor(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_read_only_descriptors(descriptors: tuple[int, ...]) -> None:
    if os.name != "posix":
        return
    for descriptor in descriptors:
        _require_read_only_descriptor(descriptor)


def _require_read_only_descriptor(descriptor: int) -> None:
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError as error:
        raise ValueError("pass_fds must contain open descriptors") from error
    if flags & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError("pass_fds must contain read-only descriptors")


def _spawn_options(cwd: Path, env: Mapping[str, str]) -> dict[str, object]:
    return {
        "cwd": cwd,
        "env": env,
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }


def _collect_cleanup_error(errors: list[BaseException], action) -> None:
    try:
        action()
    except BaseException as cleanup_error:  # noqa: BLE001 - cleanup is best effort
        errors.append(cleanup_error)


def _reaped_or_recorded(
    process: subprocess.Popen[bytes], errors: list[BaseException]
) -> bool:
    try:
        return process.poll() is not None
    except BaseException as cleanup_error:  # noqa: BLE001 - cleanup is best effort
        errors.append(cleanup_error)
        return False


def _deadline(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(value):
        raise ValueError("deadline must be finite")
    return float(value)


def _proc_stat_payload(entry_path: str) -> bytes | None:
    """Bounded `/proc/<pid>/stat` bytes; None when it cannot be trusted."""
    try:
        with open(Path(entry_path) / "stat", "rb") as stat_file:
            payload = stat_file.read(_LINUX_PROC_STAT_LIMIT + 1)
    except FileNotFoundError:
        return b""
    except OSError:
        return None
    if len(payload) > _LINUX_PROC_STAT_LIMIT:
        return None
    return payload


def _proc_stat_fields(payload: bytes) -> list[bytes] | None:
    closing = payload.rfind(b") ")
    if closing < 0:
        return None
    fields = payload[closing + 2 :].split()
    if len(fields) < 4:
        return None
    return fields


def _stat_belongs_to_live_group(fields: list[bytes], group: int) -> bool | None:
    """True when this entry keeps the group alive; None when unreadable."""
    try:
        process_group = int(fields[2])
        session = int(fields[3])
    except ValueError:
        return None
    if process_group != group and session != group:
        return False
    return fields[0] not in _LINUX_DEAD_STATES


def _proc_entry_keeps_group(entry_path: str, group: int) -> bool | None:
    payload = _proc_stat_payload(entry_path)
    if payload is None:
        return None
    if not payload:
        return False
    fields = _proc_stat_fields(payload)
    if fields is None:
        return None
    return _stat_belongs_to_live_group(fields, group)


def _scan_proc_entries(entries, group: int) -> bool | None:
    """True when nothing in /proc keeps the group alive; None when unreadable."""
    scanned = 0
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        scanned += 1
        if scanned > _LINUX_PROC_SCAN_LIMIT:
            return None
        verdict = _proc_entry_verdict(entry.path, group)
        if verdict is not None:
            return verdict
    return True


def _proc_entry_verdict(entry_path: str, group: int) -> bool | None:
    """None to keep scanning; False when live; None-unreadable maps to None."""
    keeps = _proc_entry_keeps_group(entry_path, group)
    if keeps is None:
        return None
    if keeps:
        return False
    return None


def _linux_group_is_inert(proc_root: Path, group: int) -> bool | None:
    try:
        entries = os.scandir(proc_root)
    except OSError:
        return None
    with entries:
        try:
            return _scan_proc_entries(entries, group)
        except OSError:
            return None


def _observe_posix_tree(
    process: subprocess.Popen[bytes], group: int
) -> tuple[bool, bool]:
    direct_reaped = process.poll() is not None
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return direct_reaped, True
    except PermissionError:
        # macOS answers EPERM when the group still holds a process this caller
        # may not signal — a zombie awaiting its parent, typically. The group
        # exists either way, which is the only thing this probe asks.
        return direct_reaped, False
    if direct_reaped and sys.platform.startswith("linux"):
        return direct_reaped, _linux_group_is_inert(Path("/proc"), group) is True
    return direct_reaped, False


def _wait_posix_tree(
    process: subprocess.Popen[bytes], group: int, deadline: float
) -> bool:
    while True:
        direct_reaped, group_absent = _observe_posix_tree(process, group)
        if direct_reaped and group_absent:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


# Windows helpers are defined unconditionally and called only from the
# `os.name == "nt"` branches above. Keeping them at module level avoids an
# artificial nesting level; the ctypes bindings they use stay inside the
# platform guard and are resolved when a Windows caller runs them.
def _assign_windows_process(job: int, process: subprocess.Popen[bytes]) -> None:
    if not _KERNEL32.AssignProcessToJobObject(job, int(process._handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _job_active_processes(job: int) -> int:
    information = _BasicAccountingInformation()
    returned = wintypes.DWORD()
    if not _KERNEL32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ctypes.byref(returned),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.active_processes)


def _bounded_job_active_processes(job: int) -> int:
    active = _job_active_processes(job)
    if active > _MAX_JOB_PROCESS_IDS:
        raise RuntimeError("LSP Windows Job active process bound was exceeded")
    return active


def _job_process_ids(job: int) -> tuple[int, ...]:
    _bounded_job_active_processes(job)
    offset = _BasicProcessIdList.process_id_list.offset
    buffer_size = offset + _MAX_JOB_PROCESS_IDS * ctypes.sizeof(ctypes.c_size_t)
    buffer = ctypes.create_string_buffer(buffer_size)
    information = _BasicProcessIdList.from_buffer(buffer)
    returned = wintypes.DWORD()
    queried = _KERNEL32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        ctypes.byref(buffer),
        buffer_size,
        ctypes.byref(returned),
    )
    captured = int(information.number_of_process_ids_in_list) if queried else 0
    if captured <= _MAX_JOB_PROCESS_IDS:
        values = (ctypes.c_size_t * captured).from_buffer(buffer, offset)
        process_ids = tuple(int(process_id) for process_id in values)
    else:
        process_ids = ()
    _bounded_job_active_processes(job)
    return process_ids


def _windows_wait_result_alive(result: int) -> bool:
    if result == _WAIT_OBJECT_0:
        return False
    if result == _WAIT_TIMEOUT:
        return True
    if result == _WAIT_FAILED:
        raise ctypes.WinError(ctypes.get_last_error())
    raise OSError(f"unexpected Windows process wait result: {result}")


def _windows_pid_alive(pid: int) -> bool:
    handle = _KERNEL32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists.
            return False
        raise ctypes.WinError(error)
    try:
        return _windows_wait_result_alive(
            int(_KERNEL32.WaitForSingleObject(handle, 0))
        )
    finally:
        _close_windows_handle(int(handle))


def _wait_windows_tree(
    process: subprocess.Popen[bytes],
    job: int,
    deadline: float,
    *,
    tracked_pids: Sequence[int] = (),
) -> tuple[bool, list[BaseException]]:
    errors: list[BaseException] = []
    state = _WindowsWaitState(remaining_pids=set(tracked_pids))
    while True:
        direct_reaped = _observe_direct_process(process, state)
        active, state.query_failed = _job_active_once(job, errors, state.query_failed)
        _prune_dead_pids(state.remaining_pids)
        if _tree_settled(direct_reaped, active, state):
            return True, _final_errors(errors, state.wait_error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, _final_errors(errors, state.wait_error)
        _wait_one_step(process, direct_reaped, deadline, remaining, state)


def _observe_direct_process(
    process: subprocess.Popen[bytes], state: _WindowsWaitState
) -> bool:
    direct_reaped = process.poll() is not None
    if direct_reaped:
        state.wait_error = None
    return direct_reaped


def _job_active_once(
    job: int, errors: list[BaseException], query_failed: bool
) -> tuple[int | None, bool]:
    """Active count, or None once the query has failed for this tree."""
    if query_failed:
        return None, True
    try:
        return _bounded_job_active_processes(job), False
    except BaseException as error:  # noqa: BLE001 - reported by the caller
        errors.append(error)
        return None, True


def _prune_dead_pids(remaining_pids: set[int]) -> None:
    for pid in tuple(remaining_pids):
        if not _pid_still_alive(pid):
            remaining_pids.discard(pid)


def _pid_still_alive(pid: int) -> bool:
    try:
        return _windows_pid_alive(pid)
    except BaseException:  # noqa: BLE001 - an unreadable pid stops tracking
        return False


def _pid_settled(state: _WindowsWaitState, now: float) -> bool:
    if not state.remaining_pids:
        return True
    return now - state.empty_since >= _WINDOWS_PID_SETTLE_SECONDS


def _tree_settled(
    direct_reaped: bool, active: int | None, state: _WindowsWaitState
) -> bool:
    """Two consecutive empty observations, with tracked pids settled."""
    if not direct_reaped or active != 0:
        state.reset_empty()
        return False
    now = time.monotonic()
    state.observe_empty(now)
    return state.empty_observations >= 2 and _pid_settled(state, now)


def _final_errors(
    errors: list[BaseException], wait_error: BaseException | None
) -> list[BaseException]:
    if wait_error is None:
        return errors
    return errors + [wait_error]


def _wait_one_step(
    process: subprocess.Popen[bytes],
    direct_reaped: bool,
    deadline: float,
    remaining: float,
    state: _WindowsWaitState,
) -> None:
    if direct_reaped:
        time.sleep(min(0.01, remaining))
        return
    state.wait_error = _wait_direct_process(process, deadline, remaining, state.wait_error)


def _wait_direct_process(
    process: subprocess.Popen[bytes],
    deadline: float,
    remaining: float,
    wait_error: BaseException | None,
) -> BaseException | None:
    try:
        process.wait(timeout=min(0.01, remaining))
    except subprocess.TimeoutExpired:
        return wait_error
    except BaseException as error:  # noqa: BLE001 - retried until the deadline
        left = deadline - time.monotonic()
        if left > 0:
            time.sleep(min(0.01, left))
        return error
    return None


def _create_windows_job() -> int:
    handle = _KERNEL32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    job = int(handle)
    limits = _ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not _KERNEL32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException as configuration_error:
        try:
            _close_windows_handle(job)
        except BaseException as cleanup_error:
            raise _ProcessTreeSpawnError(
                None,
                (configuration_error, cleanup_error),
                windows_job=job,
            ) from configuration_error
        raise
    return job


def _resume_one_thread(thread_id: int) -> None:
    """Resume exactly one suspended thread of the freshly created process."""
    thread = _KERNEL32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        previous_count = int(_KERNEL32.ResumeThread(thread))
        if previous_count == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous_count != 1:
            raise RuntimeError(
                "suspended LSP primary thread had an invalid suspend count"
            )
    finally:
        _close_windows_handle(int(thread))


def _advance_thread_snapshot(snapshot: int, entry: _ThreadEntry32) -> bool:
    if bool(_KERNEL32.Thread32Next(snapshot, ctypes.byref(entry))):
        return True
    error = ctypes.get_last_error()
    if error != _ERROR_NO_MORE_FILES:
        raise ctypes.WinError(error)
    return False


def _resume_threads_of(snapshot: int, entry: _ThreadEntry32, pid: int) -> int:
    resumed = 0
    available = bool(_KERNEL32.Thread32First(snapshot, ctypes.byref(entry)))
    while available:
        if int(entry.owner_process_id) == pid:
            _resume_one_thread(entry.thread_id)
            resumed += 1
        available = _advance_thread_snapshot(snapshot, entry)
    return resumed


def _resume_windows_process(pid: int) -> None:
    snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = _ThreadEntry32()
    entry.size = ctypes.sizeof(entry)
    try:
        resumed = _resume_threads_of(snapshot, entry, pid)
    finally:
        _close_windows_handle(int(snapshot))
    if resumed != 1:
        raise RuntimeError("suspended LSP process did not have one primary thread")


def _close_windows_handle(handle: int) -> None:
    if not _KERNEL32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _restore_handle_closed_flag(handle: object, was_closed: object) -> None:
    """CPython marks `Handle.closed` before calling CloseHandle."""
    if was_closed is not False or getattr(handle, "closed", None) is not True:
        return
    try:
        handle.closed = False
    except (AttributeError, TypeError):
        pass


def _close_windows_process_handle(process: subprocess.Popen[bytes]) -> None:
    handle = getattr(process, "_handle", None)
    close = getattr(handle, "Close", None)
    if not callable(close):
        return
    was_closed = getattr(handle, "closed", None)
    try:
        close()
    except BaseException:
        _restore_handle_closed_flag(handle, was_closed)
        raise


__all__ = ["ProcessTree"]
