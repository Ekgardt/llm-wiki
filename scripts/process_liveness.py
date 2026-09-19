"""One answer to "is this process alive".

Three states, one policy: `dead` when the operating system says the PID is
gone (`ESRCH`; Windows error 87 or 1168), `alive` when it is running,
`unknown` for anything the probe cannot settle — a process owned by another
user (`EPERM`), an unexpected error, a platform without a probe. The one
boolean the legacy locks need, `pid_alive`, treats doubt as alive, so a lock
is never stolen on a guess.

`process_start_identity` names the process rather than its number — the boot
id and start ticks on Linux, the start time on macOS, the creation FILETIME on
Windows — so a reused PID is not mistaken for the owner that died. It lives
here, beside the PID probe and with no dependency of its own, because every
lock file that records it (`run/state.json.lock`, `run/maintenance.lock`,
`run/compile.pid`) is read on the hook path, where importing the ownership
registry would cost the coordinator's whole module tree.
`owner_alive(pid, identity)` is the one question those locks ask.
Research: docs/research/2026-09-11-one-answer-to-is-this-process-alive.md,
docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import sys
from pathlib import Path

_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_GONE_ERRORS = frozenset({87, 1168})
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PROCESS_STAT_BYTES = 8192


class ProcessIdentityUnavailable(OSError):
    """This platform has no process-start-identity probe."""


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


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("files", ctypes.c_uint32),
        ("process_group", ctypes.c_uint32),
        ("job_control", ctypes.c_uint32),
        ("tty_device", ctypes.c_uint32),
        ("tty_process_group", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    )


def _platform_system() -> str:
    return platform.system()


def _read_bounded_system_file(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise OSError(f"system process file exceeded {maximum} bytes")
    return content


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _linux_stat_fields(pid: int, raw: bytes) -> list[bytes]:
    closing = raw.rfind(b")")
    prefix = f"{pid} (".encode("ascii")
    if not raw.startswith(prefix) or closing < len(prefix):
        raise OSError("Linux process stat was malformed")
    fields = raw[closing + 1 :].split()
    if len(fields) <= 19:
        raise OSError("Linux process stat was incomplete")
    return fields


def _linux_start_ticks(fields: list[bytes]) -> int:
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise OSError("Linux process identity was malformed") from exc
    if start_ticks <= 0:
        raise OSError("Linux process identity was malformed")
    return start_ticks


def _linux_boot_id() -> str:
    try:
        boot_id = (
            _read_bounded_system_file(Path("/proc/sys/kernel/random/boot_id"), 128)
            .decode("ascii", errors="strict")
            .strip()
        )
    except UnicodeError as exc:
        raise OSError("Linux process identity was malformed") from exc
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", boot_id):
        raise OSError("Linux process identity was malformed")
    return boot_id.lower()


def _linux_process_start_identity(pid: int) -> str | None:
    try:
        raw = _read_bounded_system_file(
            Path(f"/proc/{pid}/stat"), _MAX_PROCESS_STAT_BYTES
        )
    except FileNotFoundError:
        return None
    fields = _linux_stat_fields(pid, raw)
    if fields[0] in {b"Z", b"X", b"x"}:
        return None
    start_ticks = _linux_start_ticks(fields)
    return f"linux:{_linux_boot_id()}:{start_ticks}"


def _windows_process_api(kernel32: object) -> tuple[object, object, object, object]:
    """Bind the kernel32 entry points this probe uses, with exact signatures."""
    from ctypes import wintypes

    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return open_process, get_exit_code, get_process_times, close_handle


def _windows_open_refusal() -> None:
    """A refused open means a missing process only for the two 'no such pid' errors."""
    error = ctypes.get_last_error()
    if error in _WINDOWS_GONE_ERRORS:
        return None
    raise ctypes.WinError(error)


def _windows_creation_filetime(handle: object, get_process_times: object) -> int:
    from ctypes import wintypes

    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not get_process_times(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    created = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    if created <= 0:
        raise OSError("Windows process creation time was unavailable")
    return created


def _windows_running_identity(
    handle: object, get_exit_code: object, get_process_times: object
) -> str | None:
    from ctypes import wintypes

    exit_code = wintypes.DWORD()
    if not get_exit_code(handle, ctypes.byref(exit_code)):
        raise ctypes.WinError(ctypes.get_last_error())
    if exit_code.value != _WINDOWS_STILL_ACTIVE:
        return None
    return f"windows:{_windows_creation_filetime(handle, get_process_times)}"


def _windows_process_start_identity(pid: int) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process, get_exit_code, get_process_times, close_handle = (
        _windows_process_api(kernel32)
    )
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _windows_open_refusal()
    try:
        return _windows_running_identity(handle, get_exit_code, get_process_times)
    finally:
        close_handle(handle)


# `proc_pidinfo(PROC_PIDTBSDINFO, arg=1)` finds a zombie too: with arg 0 the
# kernel answers ESRCH for a process that died but has not been waited for,
# `kill(pid, 0)` then succeeds, and the lease of a dead owner could never be
# reclaimed. `p_stat == SZOMB` is the same "gone" that Linux state `Z` is.
# XNU bsd/kern/proc_info.c, bsd/sys/proc.h. Research:
# docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_FIND_ZOMBIE = 1
_DARWIN_SZOMB = 5


def _darwin_proc_pidinfo(pid: int, information: object, size: int) -> int:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    function.restype = ctypes.c_int
    return function(
        pid, _DARWIN_PROC_PIDTBSDINFO, _DARWIN_FIND_ZOMBIE, ctypes.byref(information), size
    )


def _darwin_absent_process(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise OSError("Darwin process identity was inaccessible") from exc
    raise OSError("Darwin process identity was unavailable")


def _require_darwin_information(
    information: object, pid: int, *, fully_sized: bool
) -> None:
    if not fully_sized or information.pid != pid:
        raise OSError("Darwin process identity was malformed")
    if information.start_seconds <= 0 or information.start_microseconds >= 1_000_000:
        raise OSError("Darwin process start time was unavailable")


def _darwin_process_start_identity(pid: int) -> str | None:
    information = _DarwinProcBsdInfo()
    size = ctypes.sizeof(information)
    result = _darwin_proc_pidinfo(pid, information, size)
    if result <= 0:
        return _darwin_absent_process(pid)
    if information.status == _DARWIN_SZOMB:
        return None
    _require_darwin_information(information, pid, fully_sized=result == size)
    return f"darwin:{information.start_seconds}:{information.start_microseconds}"


# Held by name, not by value: the probe is resolved at call time so that
# substituting one platform's probe substitutes what this dispatch calls.
_PROCESS_IDENTITY_PROBES = {
    "Windows": "_windows_process_start_identity",
    "Linux": "_linux_process_start_identity",
    "Darwin": "_darwin_process_start_identity",
}


def _require_pid(pid: object) -> None:
    if not _is_plain_int(pid) or pid <= 0:
        raise ValueError("pid must be a positive integer")


def process_start_identity(pid: int) -> str | None:
    """Return the OS process-start identity, or ``None`` for a missing process."""
    _require_pid(pid)
    probe = _PROCESS_IDENTITY_PROBES.get(_platform_system())
    if probe is None:
        raise ProcessIdentityUnavailable("operational process identity is unsupported")
    return globals()[probe](pid)


def owner_alive(pid: object, start_identity: str | None = None) -> bool:
    """Whether the process that took a lock still holds it; doubt says yes.

    With the identity the owner recorded, a PID the operating system has since
    handed to somebody else reads as dead — which is the whole point of writing
    it down. Without one (a lock file from before this release) the answer is
    the PID probe, exactly as it was.
    """
    if not start_identity:
        return pid_alive(pid)
    observed, settled = _observed_identity(pid)
    if not settled:
        return True
    return observed == start_identity


def _observed_identity(pid: object) -> tuple[str | None, bool]:
    """(the identity this PID carries now, whether the probe settled it)."""
    try:
        return (process_start_identity(pid), True)
    except (OSError, PermissionError, ValueError):
        return (None, False)
