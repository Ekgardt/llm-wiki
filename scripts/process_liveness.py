"""One answer to "is this process alive".

Three states, one policy: `dead` when the operating system says the PID is
gone (`ESRCH`; Windows error 87 or 1168), `alive` when it is running,
`unknown` for anything the probe cannot settle — a process owned by another
user (`EPERM`), an unexpected error, a platform without a probe. The one
boolean the legacy locks need, `pid_alive`, treats doubt as alive, so a lock
is never stolen on a guess. Owners that recorded a process start identity are
probed by `operational_ownership.process_identity_state`, which is PID-reuse
safe; a PID-only marker cannot be. Research:
docs/research/2026-09-11-one-answer-to-is-this-process-alive.md
"""

from __future__ import annotations

import errno
import os
import sys

_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_GONE_ERRORS = frozenset({87, 1168})
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _windows_open_failure_state(last_error: int) -> str:
    if last_error in _WINDOWS_GONE_ERRORS:
        return "dead"
    return "unknown"


def _windows_exit_code_state(ctypes, wintypes, get_exit_code, handle) -> str:
    exit_code = wintypes.DWORD()
    if not get_exit_code(handle, ctypes.byref(exit_code)):
        return "unknown"
    if exit_code.value == _WINDOWS_STILL_ACTIVE:
        return "alive"
    return "dead"


def _windows_process_state(pid: int) -> str:
    """Ask the kernel directly; a missing process is dead, anything else unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return _windows_open_failure_state(ctypes.get_last_error())
        try:
            return _windows_exit_code_state(ctypes, wintypes, get_exit_code, handle)
        finally:
            close_handle(handle)
    except (AttributeError, OSError, OverflowError, ValueError):
        return "unknown"


def _os_error_process_state(exc: OSError) -> str:
    if exc.errno == errno.ESRCH:
        return "dead"
    return "unknown"


def _posix_process_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as exc:
        return _os_error_process_state(exc)
    except (OverflowError, ValueError):
        return "unknown"
    return "alive"


def process_state(pid: object) -> str:
    """`alive`, `dead` or `unknown` for a PID; anything but a positive int is unknown."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unknown"
    if sys.platform == "win32":
        return _windows_process_state(pid)
    return _posix_process_state(pid)


def pid_alive(pid: object) -> bool:
    """The one boolean a legacy lock may ask: only a provably dead process is dead."""
    return process_state(pid) != "dead"
