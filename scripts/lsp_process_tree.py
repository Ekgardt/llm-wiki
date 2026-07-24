"""Cross-platform ownership and bounded cleanup of one LSP process tree."""

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

_LINUX_PROC_SCAN_LIMIT = 131_072
_LINUX_PROC_STAT_LIMIT = 4096
_LINUX_DEAD_STATES = frozenset({b"Z", b"X", b"x"})

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _MAX_JOB_PROCESS_IDS = 4096
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
        return cls._spawn_with_deadline(
            command,
            cwd=cwd,
            env=env,
            deadline=time.monotonic() + 2.0,
        )

    @classmethod
    def _spawn_with_deadline(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        deadline: float,
    ) -> ProcessTree:
        deadline = _deadline(deadline)
        options: dict[str, object] = {
            "cwd": cwd,
            "env": env,
            "shell": False,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "close_fds": True,
        }
        if os.name == "posix":
            process = subprocess.Popen(list(command), start_new_session=True, **options)
            return cls(process, None, process.pid)
        if os.name != "nt":
            raise RuntimeError("LSP process trees are unsupported on this platform")

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
                raise

            tree = cls(process, job, None)
            cleanup_errors: list[BaseException] = []
            try:
                tree.terminate(deadline=deadline)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                tree.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                child_reaped = process.poll() is not None
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
                child_reaped = False
            if tree.windows_job is not None or not child_reaped:
                raise _ProcessTreeSpawnError(tree, tuple(cleanup_errors)) from setup_error
            raise

    def terminate(self, *, deadline: float) -> None:
        deadline = _deadline(deadline)
        if os.name == "nt":
            self._terminate_windows(deadline)
            return
        self._terminate_posix(deadline)

    def _terminate_posix(self, deadline: float) -> None:
        group = self.process_group
        if group is None:
            raise RuntimeError("POSIX LSP process group ownership was released")
        errors: list[BaseException] = []
        started = time.monotonic()
        graceful_deadline = min(
            deadline,
            started + min(0.5, max(0.0, (deadline - started) / 2)),
        )
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except BaseException as error:
            errors.append(error)

        complete = _wait_posix_tree(self.process, group, graceful_deadline)
        if not complete:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except BaseException as error:
                errors.append(error)
            complete = _wait_posix_tree(self.process, group, deadline)

        if errors:
            raise errors[0]
        if not complete:
            raise TimeoutError("LSP process group did not exit before deadline")

    def _terminate_windows(self, deadline: float) -> None:
        job = self.windows_job
        if job is None:
            raise RuntimeError("Windows LSP process tree ownership was released")
        errors: list[BaseException] = []
        tracked_pids: tuple[int, ...] = ()
        try:
            tracked_pids = _job_process_ids(job)
        except BaseException as error:
            errors.append(error)
        try:
            if not _KERNEL32.TerminateJobObject(job, 1):
                errors.append(ctypes.WinError(ctypes.get_last_error()))
        except BaseException as error:
            errors.append(error)

        try:
            direct_reaped = self.process.poll() is not None
        except BaseException as error:
            errors.append(error)
            direct_reaped = False
        if not direct_reaped:
            try:
                self.process.kill()
            except BaseException as error:
                errors.append(error)
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            errors.append(error)

        try:
            complete, observe_errors = _wait_windows_tree(
                self.process,
                job,
                deadline,
                tracked_pids=tracked_pids,
            )
        except BaseException as error:
            complete = False
            errors.append(error)
        else:
            errors.extend(observe_errors)
        if errors:
            raise errors[0]
        if not complete:
            raise TimeoutError("LSP Windows process tree did not exit before deadline")

    def close(self) -> None:
        if os.name == "nt":
            job = self.windows_job
            if job is None:
                return
            errors: list[BaseException] = []
            try:
                direct_reaped = self.process.poll() is not None
            except BaseException as error:
                errors.append(error)
                direct_reaped = False
            try:
                active = _job_active_processes(job)
            except BaseException as error:
                errors.append(error)
                active = None
            if errors:
                raise errors[0]
            if not direct_reaped or active != 0:
                raise RuntimeError("Windows LSP process tree is still live")
            _close_windows_handle(job)
            self.windows_job = None
            return

        group = self.process_group
        if group is None:
            return
        direct_reaped, group_absent = _observe_posix_tree(self.process, group)
        if not direct_reaped or not group_absent:
            raise RuntimeError("POSIX LSP process group is still live")
        self.process_group = None


def _deadline(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(value):
        raise ValueError("deadline must be finite")
    return float(value)


def _linux_group_is_inert(proc_root: Path, group: int) -> bool | None:
    scanned = 0
    try:
        entries = os.scandir(proc_root)
    except OSError:
        return None
    with entries:
        try:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                scanned += 1
                if scanned > _LINUX_PROC_SCAN_LIMIT:
                    return None
                try:
                    with open(Path(entry.path) / "stat", "rb") as stat_file:
                        payload = stat_file.read(_LINUX_PROC_STAT_LIMIT + 1)
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                if len(payload) > _LINUX_PROC_STAT_LIMIT:
                    return None
                closing = payload.rfind(b") ")
                fields = payload[closing + 2 :].split() if closing >= 0 else ()
                if len(fields) < 4:
                    return None
                try:
                    process_group = int(fields[2])
                    session = int(fields[3])
                except ValueError:
                    return None
                if (process_group == group or session == group) and (
                    fields[0] not in _LINUX_DEAD_STATES
                ):
                    return False
        except OSError:
            return None
    return True


def _observe_posix_tree(
    process: subprocess.Popen[bytes], group: int
) -> tuple[bool, bool]:
    direct_reaped = process.poll() is not None
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        group_absent = True
    else:
        group_absent = False
        if direct_reaped and sys.platform.startswith("linux"):
            group_absent = _linux_group_is_inert(Path("/proc"), group) is True
    return direct_reaped, group_absent


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


if os.name == "nt":
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


    def _job_process_ids(job: int) -> tuple[int, ...]:
        offset = _BasicProcessIdList.process_id_list.offset
        buffer_size = offset + _MAX_JOB_PROCESS_IDS * ctypes.sizeof(ctypes.c_size_t)
        buffer = ctypes.create_string_buffer(buffer_size)
        information = _BasicProcessIdList.from_buffer(buffer)
        returned = wintypes.DWORD()
        if not _KERNEL32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(buffer),
            buffer_size,
            ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        assigned = int(information.number_of_assigned_processes)
        captured = int(information.number_of_process_ids_in_list)
        if assigned != captured or captured > _MAX_JOB_PROCESS_IDS:
            raise RuntimeError("LSP Windows Job process identifier bound was exceeded")
        process_ids = (ctypes.c_size_t * captured).from_buffer(buffer, offset)
        return tuple(int(process_id) for process_id in process_ids)


    def _windows_pid_alive(pid: int) -> bool:
        handle = _KERNEL32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists.
                return False
            raise ctypes.WinError(error)
        try:
            result = int(_KERNEL32.WaitForSingleObject(handle, 0))
            if result == _WAIT_OBJECT_0:
                return False
            if result == _WAIT_TIMEOUT:
                return True
            if result == _WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            raise OSError(f"unexpected Windows process wait result: {result}")
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
        query_failed = False
        empty_observations = 0
        remaining_pids = set(tracked_pids)
        while True:
            direct_reaped = process.poll() is not None
            active: int | None = None
            if not query_failed:
                try:
                    active = _job_active_processes(job)
                except BaseException as error:
                    errors.append(error)
                    query_failed = True
            pid_probe_failed = False
            for pid in tuple(remaining_pids):
                try:
                    alive = _windows_pid_alive(pid)
                except BaseException as error:
                    errors.append(error)
                    pid_probe_failed = True
                    break
                if not alive:
                    remaining_pids.remove(pid)
            if (
                direct_reaped
                and active == 0
                and not remaining_pids
                and not pid_probe_failed
            ):
                empty_observations += 1
                if empty_observations >= 2:
                    return True, errors
            else:
                empty_observations = 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, errors
            if not direct_reaped:
                try:
                    process.wait(timeout=min(0.01, remaining))
                except subprocess.TimeoutExpired:
                    pass
                except BaseException as error:
                    errors.append(error)
            else:
                time.sleep(min(0.01, remaining))


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


    def _resume_windows_process(pid: int) -> None:
        snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = 0
        entry = _ThreadEntry32()
        entry.size = ctypes.sizeof(entry)
        try:
            available = bool(_KERNEL32.Thread32First(snapshot, ctypes.byref(entry)))
            while available:
                if int(entry.owner_process_id) == pid:
                    thread = _KERNEL32.OpenThread(
                        _THREAD_SUSPEND_RESUME, False, entry.thread_id
                    )
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
                        resumed += 1
                    finally:
                        _close_windows_handle(int(thread))
                available = bool(_KERNEL32.Thread32Next(snapshot, ctypes.byref(entry)))
                if not available:
                    error = ctypes.get_last_error()
                    if error != _ERROR_NO_MORE_FILES:
                        raise ctypes.WinError(error)
        finally:
            _close_windows_handle(int(snapshot))
        if resumed != 1:
            raise RuntimeError("suspended LSP process did not have one primary thread")


    def _close_windows_handle(handle: int) -> None:
        if not _KERNEL32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


__all__ = ["ProcessTree"]
