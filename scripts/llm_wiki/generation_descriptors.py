"""Opening, identifying and hashing generation files without following links."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

from llm_wiki.generation_fields import check_cancelled, check_monotonic_deadline
from llm_wiki.generation_limits import HASH_CHUNK_BYTES
from reliable_memory import validate_runtime_file

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        )

    class _FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
        )

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("status", ctypes.c_ssize_t),
            ("information", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_file_information_by_handle_ex = _kernel32.GetFileInformationByHandleEx
    _get_file_information_by_handle_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _get_file_information_by_handle_ex.restype = wintypes.BOOL
    _get_file_information_by_handle = _kernel32.GetFileInformationByHandle
    _get_file_information_by_handle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information_by_handle.restype = wintypes.BOOL
    create_file = _kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = _kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _nt_open_file = _ntdll.NtOpenFile
    _nt_open_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.ULONG,
        wintypes.ULONG,
    )
    _nt_open_file.restype = ctypes.c_long
    _rtl_nt_status_to_dos_error = _ntdll.RtlNtStatusToDosError
    _rtl_nt_status_to_dos_error.argtypes = (ctypes.c_long,)
    _rtl_nt_status_to_dos_error.restype = wintypes.ULONG


def is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _stable_file_stat(metadata: os.stat_result) -> tuple[int, ...]:
    """Return identity and mutation-sensitive fields unaffected by reads."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        getattr(metadata, "st_file_attributes", 0),
        getattr(metadata, "st_reparse_tag", 0),
    )


def _require_win32(succeeded: object) -> None:
    """Raise the last Win32 error when an API reports failure."""
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_basic_information(handle: int) -> _FileBasicInfo:
    information = _FileBasicInfo()
    _require_win32(
        _get_file_information_by_handle_ex(
            handle,
            0,  # FileBasicInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )
    return information


def _windows_file_id_information(handle: int) -> _FileIdInfo:
    information = _FileIdInfo()
    _require_win32(
        _get_file_information_by_handle_ex(
            handle,
            18,  # FileIdInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )
    return information


def _windows_change_time(descriptor: int) -> int | None:
    if os.name != "nt":
        return None
    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("invalid Windows file handle")
    return int(_windows_basic_information(handle).change_time)


def _windows_handle_file_identity(handle: int) -> tuple[str, int, bytes]:
    if os.name != "nt":
        raise OSError("Windows handle identity is unavailable")
    information = _windows_file_id_information(handle)
    file_id = bytes(information.file_id.identifier)
    volume = int(information.volume_serial_number)
    if volume <= 0 or not any(file_id):
        raise OSError("Windows stable file identity is unavailable")
    return ("windows", volume, file_id)


def _windows_entry_kind(attributes: int) -> str:
    if attributes & 0x00000400:
        return "link"
    if attributes & 0x00000010:
        return "directory"
    return "file"


def _posix_file_identity(descriptor: int) -> tuple[object, ...]:
    metadata = os.fstat(descriptor)
    if metadata.st_dev <= 0 or metadata.st_ino <= 0:
        raise OSError("POSIX stable file identity is unavailable")
    return ("posix", metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _windows_descriptor_identity(descriptor: int) -> tuple[object, ...]:
    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("invalid Windows file handle")
    return _windows_handle_file_identity(handle)


def _descriptor_file_identity(descriptor: int) -> tuple[object, ...]:
    if os.name == "nt":
        return _windows_descriptor_identity(descriptor)
    if os.name == "posix":
        return _posix_file_identity(descriptor)
    raise OSError("stable file identity is unavailable")


def stable_descriptor_state(descriptor: int) -> tuple[object, ...]:
    return (
        *_stable_file_stat(os.fstat(descriptor)),
        _descriptor_file_identity(descriptor),
        _windows_change_time(descriptor),
    )


def windows_long_path(path: Path) -> str:
    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def windows_read_handle(path: Path) -> int:
    handle = create_file(
        windows_long_path(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read, write, and delete
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _closed_on_failure(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def windows_read_descriptor(path: Path) -> int:
    handle = windows_read_handle(path)
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        close_handle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        _closed_on_failure(descriptor)
        raise
    return descriptor


def open_read_descriptor(path: Path) -> int:
    if os.name == "nt":
        return windows_read_descriptor(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def hash_descriptor(
    descriptor: int,
    max_bytes: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        check_cancelled(cancelled)
        check_monotonic_deadline(deadline, monotonic)
        chunk = os.read(descriptor, HASH_CHUNK_BYTES)
        check_monotonic_deadline(deadline, monotonic)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("artifact exceeds its declared size")
        digest.update(chunk)
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    return total, digest.hexdigest()


def _hashed_descriptor(
    descriptor: int,
    expected: os.stat_result,
    max_bytes: int,
    **stop: object,
) -> tuple[int, str, os.stat_result]:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
        raise PermissionError("artifact identity changed while opening")
    opened_state = stable_descriptor_state(descriptor)
    total, digest = hash_descriptor(descriptor, max_bytes, **stop)
    after = os.fstat(descriptor)
    if opened_state != stable_descriptor_state(descriptor):
        raise PermissionError("artifact changed while hashing")
    return total, digest, after


def hash_artifact(
    path: Path,
    state_root: Path,
    max_bytes: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    expected = validate_runtime_file(path, state_root, max_bytes=max_bytes)
    descriptor = open_read_descriptor(path)
    try:
        total, digest, after = _hashed_descriptor(
            descriptor,
            expected,
            max_bytes,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
    finally:
        os.close(descriptor)
    current = path.lstat()
    if is_link_or_reparse(path) or not os.path.samestat(after, current):
        raise PermissionError("artifact identity changed after hashing")
    check_monotonic_deadline(deadline, monotonic)
    check_cancelled(cancelled)
    return total, digest
