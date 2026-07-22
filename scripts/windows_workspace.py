"""Windows handle-relative filesystem operations for sealed workspaces."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class WindowsEntry:
    name: str
    kind: str
    file_id: bytes


_API = None
_API_ERROR = "Windows native workspace APIs are unavailable on this platform"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_LIST_DIRECTORY = 0x00000001
    _FILE_READ_DATA = 0x00000001
    _FILE_WRITE_DATA = 0x00000002
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_WRITE_ATTRIBUTES = 0x00000100
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_CREATE = 2
    _FILE_OPEN = 1
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

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

    class _FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
        )

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        )

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = (
            ("allocation_size", ctypes.c_longlong),
            ("end_of_file", ctypes.c_longlong),
            ("number_of_links", wintypes.DWORD),
            ("delete_pending", ctypes.c_ubyte),
            ("directory", ctypes.c_ubyte),
        )

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        )

    class _FileIdExtdDirInfo(ctypes.Structure):
        _fields_ = (
            ("next_entry_offset", wintypes.DWORD),
            ("file_index", wintypes.DWORD),
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("end_of_file", ctypes.c_longlong),
            ("allocation_size", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
            ("file_name_length", wintypes.DWORD),
            ("ea_size", wintypes.DWORD),
            ("reparse_point_tag", wintypes.DWORD),
            ("file_id", _FileId128),
            ("file_name", wintypes.WCHAR * 1),
        )

    class _WindowsApi:
        def __init__(self) -> None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            required = {
                "CreateFileW": kernel32,
                "CloseHandle": kernel32,
                "GetFileInformationByHandleEx": kernel32,
                "SetFileInformationByHandle": kernel32,
                "ReadFile": kernel32,
                "WriteFile": kernel32,
                "FlushFileBuffers": kernel32,
                "NtCreateFile": ntdll,
                "NtOpenFile": ntdll,
                "RtlNtStatusToDosError": ntdll,
            }
            missing = [name for name, library in required.items() if not hasattr(library, name)]
            if missing:
                raise RuntimeError(
                    "Windows sealed workspaces require native API: " + missing[0]
                )

            self.create_file = kernel32.CreateFileW
            self.create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            self.create_file.restype = wintypes.HANDLE
            self.close_handle = kernel32.CloseHandle
            self.close_handle.argtypes = (wintypes.HANDLE,)
            self.close_handle.restype = wintypes.BOOL
            self.get_information = kernel32.GetFileInformationByHandleEx
            self.get_information.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            self.get_information.restype = wintypes.BOOL
            self.set_information = kernel32.SetFileInformationByHandle
            self.set_information.argtypes = self.get_information.argtypes
            self.set_information.restype = wintypes.BOOL
            self.read_file = kernel32.ReadFile
            self.read_file.argtypes = (
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            )
            self.read_file.restype = wintypes.BOOL
            self.write_file = kernel32.WriteFile
            self.write_file.argtypes = self.read_file.argtypes
            self.write_file.restype = wintypes.BOOL
            self.flush_file_buffers = kernel32.FlushFileBuffers
            self.flush_file_buffers.argtypes = (wintypes.HANDLE,)
            self.flush_file_buffers.restype = wintypes.BOOL
            self.nt_create_file = ntdll.NtCreateFile
            self.nt_create_file.argtypes = (
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.DWORD,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_IoStatusBlock),
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.LPVOID,
                wintypes.ULONG,
            )
            self.nt_create_file.restype = ctypes.c_long
            self.nt_open_file = ntdll.NtOpenFile
            self.nt_open_file.argtypes = (
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.DWORD,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_IoStatusBlock),
                wintypes.ULONG,
                wintypes.ULONG,
            )
            self.nt_open_file.restype = ctypes.c_long
            self.status_to_error = ntdll.RtlNtStatusToDosError
            self.status_to_error.argtypes = (ctypes.c_long,)
            self.status_to_error.restype = wintypes.ULONG

    try:
        _API = _WindowsApi()
        _API_ERROR = None
    except (AttributeError, OSError, RuntimeError) as exc:
        _API_ERROR = str(exc)


def capability() -> tuple[bool, str | None]:
    """Return whether the required native boundary is available and why not."""
    return _API is not None, _API_ERROR


def require_capability() -> None:
    supported, reason = capability()
    if not supported:
        raise RuntimeError(reason or "Windows native workspace APIs are unavailable")


def _component(name: str) -> str:
    try:
        encoded = name.encode("utf-16-le") if isinstance(name, str) else b""
    except UnicodeEncodeError as exc:
        raise ValueError(
            "Windows relative name must be one normalized path component"
        ) from exc
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(character in '<>:"|?*' or ord(character) < 32 for character in name)
        or name[-1] in {".", " "}
        or name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        or len(encoded) > 65534
        or unicodedata.normalize("NFC", name) != name
    ):
        raise ValueError("Windows relative name must be one normalized path component")
    return name


def close_handle(handle: int) -> None:
    require_capability()
    if not _API.close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _attributes(handle: int) -> int:
    information = _FileAttributeTagInfo()
    if not _API.get_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.file_attributes)


def identity(handle: int, *, directory: bool | None = None) -> tuple[int, bytes, bool]:
    require_capability()
    attributes = _attributes(handle)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("Windows workspace component is a reparse point")
    actual_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if directory is not None and actual_directory != directory:
        raise PermissionError("Windows workspace component has the wrong kind")
    information = _FileIdInfo()
    if not _API.get_information(
        handle, 18, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    file_id = bytes(information.file_id.identifier)
    if not any(file_id):
        raise OSError("Windows stable FILE_ID_INFO identity is unavailable")
    return int(information.volume_serial_number), file_id, actual_directory


def _object_attributes(parent: int, name: str):
    normalized = _component(name)
    name_buffer = ctypes.create_unicode_buffer(normalized)
    name_bytes = len(normalized.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        name_bytes,
        name_bytes + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    return name_buffer, unicode_name, attributes


def _relative_handle(
    parent: int, name: str, *, directory: bool, create: bool, writable: bool = False
) -> int:
    require_capability()
    name_buffer, unicode_name, attributes = _object_attributes(parent, name)
    handle = wintypes.HANDLE()
    io_status = _IoStatusBlock()
    desired = _SYNCHRONIZE | _FILE_READ_ATTRIBUTES
    if directory:
        desired |= _FILE_LIST_DIRECTORY
    else:
        desired |= _FILE_READ_DATA
    if create or writable:
        desired |= _FILE_WRITE_ATTRIBUTES
    if writable:
        desired |= _FILE_WRITE_DATA
    options = (
        _FILE_OPEN_REPARSE_POINT
        | _FILE_SYNCHRONOUS_IO_NONALERT
        | (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
    )
    share = 0 if not directory else _FILE_SHARE_READ | _FILE_SHARE_WRITE
    if create:
        allocation = ctypes.c_longlong(0)
        status = _API.nt_create_file(
            ctypes.byref(handle),
            desired,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            ctypes.byref(allocation),
            _FILE_ATTRIBUTE_NORMAL,
            share,
            _FILE_CREATE,
            options,
            None,
            0,
        )
    else:
        status = _API.nt_open_file(
            ctypes.byref(handle),
            desired,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            share,
            options,
        )
    if status < 0:
        error = int(_API.status_to_error(status))
        if error in {80, 183}:  # ERROR_FILE_EXISTS | ERROR_ALREADY_EXISTS
            raise FileExistsError(
                error, f"cannot {'create' if create else 'open'} Windows component: {name}"
            )
        if error in {2, 3}:  # ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(
                error, f"cannot {'create' if create else 'open'} Windows component: {name}"
            )
        raise OSError(error, f"cannot {'create' if create else 'open'} Windows component: {name}")
    value = int(handle.value)
    try:
        identity(value, directory=directory)
    except BaseException:
        close_handle(value)
        raise
    return value


def create_directory(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=True, create=True)


def open_directory(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=True, create=False)


def create_file(parent: int, name: str) -> int:
    return _relative_handle(
        parent, name, directory=False, create=True, writable=True
    )


def open_file(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=False, create=False)


def open_directory_path(path: Path) -> list[int]:
    """Open a local absolute directory one no-follow component at a time."""
    require_capability()
    absolute = Path(path).absolute()
    pure = PureWindowsPath(str(absolute))
    if not pure.drive or not pure.root or str(pure).startswith("\\\\"):
        raise RuntimeError("safe Windows sealed workspaces require a local drive path")
    root_path = f"\\\\?\\{pure.drive}\\"
    root = _API.create_file(
        root_path,
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if root == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    handles = [int(root)]
    try:
        identity(handles[0], directory=True)
        for part in pure.parts[1:]:
            handles.append(open_directory(handles[-1], part))
        return handles
    except BaseException:
        for handle in reversed(handles):
            close_handle(handle)
        raise


def list_directory(handle: int, *, max_entries: int) -> list[WindowsEntry]:
    require_capability()
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
        raise ValueError("max_entries must be a non-negative integer")
    entries: list[WindowsEntry] = []
    restart = True
    buffer_size = 64 * 1024
    name_offset = _FileIdExtdDirInfo.file_name.offset
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        information_class = 20 if restart else 19
        if not _API.get_information(
            handle, information_class, ctypes.byref(buffer), buffer_size
        ):
            error = ctypes.get_last_error()
            if error in {18, 38}:  # ERROR_NO_MORE_FILES | ERROR_HANDLE_EOF
                break
            raise ctypes.WinError(error)
        offset = 0
        while True:
            information = _FileIdExtdDirInfo.from_buffer(buffer, offset)
            name_length = int(information.file_name_length)
            if (
                name_length <= 0
                or name_length % 2
                or offset + name_offset + name_length > buffer_size
            ):
                raise OSError("Windows directory enumeration returned invalid data")
            name = ctypes.wstring_at(
                ctypes.addressof(buffer) + offset + name_offset,
                name_length // 2,
            )
            if name not in {".", ".."}:
                attributes = int(information.file_attributes)
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    kind = "link"
                elif attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    kind = "directory"
                else:
                    kind = "file"
                entries.append(
                    WindowsEntry(name, kind, bytes(information.file_id.identifier))
                )
                if len(entries) > max_entries:
                    raise ValueError("sealed workspace entry range exceeded")
            next_offset = int(information.next_entry_offset)
            if next_offset == 0:
                break
            if next_offset < name_offset or offset + next_offset >= buffer_size:
                raise OSError("Windows directory enumeration returned invalid offsets")
            offset += next_offset
        restart = False
    normalized: dict[str, str] = {}
    for entry in entries:
        if unicodedata.normalize("NFC", entry.name) != entry.name:
            raise PermissionError("Windows workspace contains a non-normalized name")
        folded = entry.name.casefold()
        previous = normalized.get(folded)
        if previous is not None and previous != entry.name:
            raise PermissionError("Windows workspace contains a case-fold collision")
        normalized[folded] = entry.name
    return sorted(entries, key=lambda item: item.name)


def write_all(handle: int, content: bytes, *, chunk_bytes: int) -> None:
    if not isinstance(content, bytes):
        raise TypeError("Windows workspace content must be bytes")
    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + chunk_bytes]
        buffer = ctypes.create_string_buffer(chunk)
        written = wintypes.DWORD()
        if not _API.write_file(
            handle, buffer, len(chunk), ctypes.byref(written), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value <= 0:
            raise OSError("sealed workspace write made no progress")
        offset += int(written.value)


def read_chunks(handle: int, *, chunk_bytes: int, max_bytes: int):
    total = 0
    while True:
        buffer = ctypes.create_string_buffer(chunk_bytes)
        read = wintypes.DWORD()
        if not _API.read_file(
            handle, buffer, chunk_bytes, ctypes.byref(read), None
        ):
            error = ctypes.get_last_error()
            if error == 38:  # ERROR_HANDLE_EOF
                break
            raise ctypes.WinError(error)
        if read.value == 0:
            break
        total += int(read.value)
        if total > max_bytes:
            raise ValueError("sealed workspace file exceeded captured range")
        yield buffer.raw[: read.value]


def file_size(handle: int) -> int:
    information = _FileStandardInfo()
    if not _API.get_information(
        handle, 1, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.directory:
        raise PermissionError("Windows workspace file became a directory")
    return int(information.end_of_file)


def is_read_only(handle: int) -> bool:
    return bool(_attributes(handle) & _FILE_ATTRIBUTE_READONLY)


def set_read_only(handle: int) -> bool:
    attributes = _attributes(handle)
    information = _FileBasicInfo(0, 0, 0, 0, attributes | _FILE_ATTRIBUTE_READONLY)
    if not _API.set_information(
        handle, 0, ctypes.byref(information), ctypes.sizeof(information)
    ):
        return False
    return bool(_attributes(handle) & _FILE_ATTRIBUTE_READONLY)


def flush_file(handle: int) -> None:
    if not _API.flush_file_buffers(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def flush_directory(handle: int) -> bool:
    if _API.flush_file_buffers(handle):
        return True
    return False


__all__ = [
    "WindowsEntry",
    "capability",
    "close_handle",
    "create_directory",
    "create_file",
    "file_size",
    "flush_directory",
    "flush_file",
    "identity",
    "is_read_only",
    "list_directory",
    "open_directory",
    "open_directory_path",
    "open_file",
    "read_chunks",
    "require_capability",
    "set_read_only",
    "write_all",
]
