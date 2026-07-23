"""Cross-platform ownership and bounded cleanup of one LSP process tree."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

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


@dataclass(slots=True)
class ProcessTree:
    process: subprocess.Popen[bytes]
    windows_job: int | None
    process_group: int | None

    @classmethod
    def spawn(
        cls, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> ProcessTree:
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

        job: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            job = _create_windows_job()
            process = subprocess.Popen(
                list(command), creationflags=_CREATE_SUSPENDED, **options
            )
            if not _KERNEL32.AssignProcessToJobObject(job, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            _resume_windows_process(process.pid)
            return cls(process, job, None)
        except BaseException:
            if job is not None:
                _KERNEL32.TerminateJobObject(job, 1)
                _close_windows_handle(job)
            elif process is not None:
                try:
                    process.kill()
                except OSError:
                    pass
            if process is not None:
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            raise

    def terminate(self, *, deadline: float) -> None:
        deadline = _deadline(deadline)
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            job = self.windows_job
            if job is None:
                raise RuntimeError("Windows LSP process tree ownership was released")
            if not _KERNEL32.TerminateJobObject(job, 1):
                error = ctypes.get_last_error()
                if self.process.poll() is None:
                    raise ctypes.WinError(error)
            _wait(self.process, deadline)
            return

        group = self.process_group
        if group is None:
            raise RuntimeError("POSIX LSP process group ownership was released")
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        remaining = deadline - time.monotonic()
        graceful_deadline = time.monotonic() + min(0.5, max(0.0, remaining / 2))
        try:
            _wait(self.process, graceful_deadline)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait(self.process, deadline)

    def close(self) -> None:
        if os.name == "nt":
            job = self.windows_job
            self.windows_job = None
            if job is not None:
                _close_windows_handle(job)
            return
        group = self.process_group
        self.process_group = None
        if group is not None and self.process.poll() is None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _deadline(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(value):
        raise ValueError("deadline must be finite")
    return float(value)


def _wait(process: subprocess.Popen[bytes], deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 and process.poll() is None:
        raise TimeoutError("LSP process tree did not exit before deadline")
    try:
        process.wait(timeout=max(0.0, remaining))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("LSP process tree did not exit before deadline") from exc


if os.name == "nt":
    def _create_windows_job() -> int:
        handle = _KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        job = int(handle)
        limits = _ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _KERNEL32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            _close_windows_handle(job)
            raise ctypes.WinError(error)
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
                        if _KERNEL32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed += 1
                    finally:
                        _close_windows_handle(int(thread))
                available = bool(_KERNEL32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            _close_windows_handle(int(snapshot))
        if resumed != 1:
            raise RuntimeError("suspended LSP process did not have one primary thread")


    def _close_windows_handle(handle: int) -> None:
        if not _KERNEL32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


__all__ = ["ProcessTree"]
