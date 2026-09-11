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
_MAX_LOCAL_PATH_CHARACTERS = 32_767


@dataclass(frozen=True, slots=True)
class WindowsEntry:
    name: str
    kind: str
    file_id: bytes
    size: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or not 0 <= self.size < 2**63
        ):
            raise ValueError("Windows entry size must be a bounded non-negative integer")


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
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
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
    _GENERIC_WRITE = 0x40000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _MOVEFILE_REPLACE_EXISTING = 0x00000001
    _MOVEFILE_WRITE_THROUGH = 0x00000008

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

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = (("delete_file", wintypes.BOOL),)

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = (
            ("replace_if_exists", wintypes.BOOL),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
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
                "GetShortPathNameW": kernel32,
                "SetFileInformationByHandle": kernel32,
                "ReadFile": kernel32,
                "WriteFile": kernel32,
                "SetFilePointerEx": kernel32,
                "FlushFileBuffers": kernel32,
                "MoveFileExW": kernel32,
                "NtCreateFile": ntdll,
                "NtOpenFile": ntdll,
                "NtSetInformationFile": ntdll,
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
            self.get_short_path = kernel32.GetShortPathNameW
            self.get_short_path.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.DWORD,
            )
            self.get_short_path.restype = wintypes.DWORD
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
            self.set_file_pointer = kernel32.SetFilePointerEx
            self.set_file_pointer.argtypes = (
                wintypes.HANDLE,
                ctypes.c_longlong,
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.DWORD,
            )
            self.set_file_pointer.restype = wintypes.BOOL
            self.flush_file_buffers = kernel32.FlushFileBuffers
            self.flush_file_buffers.argtypes = (wintypes.HANDLE,)
            self.flush_file_buffers.restype = wintypes.BOOL
            self.move_file_ex = kernel32.MoveFileExW
            self.move_file_ex.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
            )
            self.move_file_ex.restype = wintypes.BOOL
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
            self.nt_set_information_file = ntdll.NtSetInformationFile
            self.nt_set_information_file.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_IoStatusBlock),
                wintypes.LPVOID,
                wintypes.ULONG,
                ctypes.c_int,
            )
            self.nt_set_information_file.restype = ctypes.c_long
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


_UNBOUNDED_PATH = "Windows short path must be a bounded local absolute path"


def _bounded_local_absolute_path(path: Path) -> tuple[str, PureWindowsPath]:
    if not isinstance(path, Path):
        raise TypeError("Windows short path must be a Path")
    value = str(path)
    pure = PureWindowsPath(value)
    characters = _utf16_characters(value)
    if not _local_absolute(value, pure) or characters > _MAX_LOCAL_PATH_CHARACTERS:
        raise ValueError(_UNBOUNDED_PATH)
    return str(pure), pure


def _utf16_characters(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError(_UNBOUNDED_PATH) from exc


def _local_absolute(value: str, pure: PureWindowsPath) -> bool:
    """A drive-rooted local path: no UNC prefix, no NUL."""
    if not pure.drive or pure.root != "\\":
        return False
    return not value.startswith("\\\\") and "\x00" not in value


_SHORT_PATH_RANGE = "Windows short path result exceeded the bounded path range"


def get_short_path(path: Path) -> Path:
    """Return the bounded 8.3 form of one existing local absolute path."""
    require_capability()
    value, pure = _bounded_local_absolute_path(path)
    required = _short_path_length(value)
    result, result_pure = _bounded_local_absolute_path(Path(_short_path_text(value, required)))
    if result_pure.drive.casefold() != pure.drive.casefold():
        raise OSError("Windows short path changed the local drive")
    return Path(result)


def _short_path_length(value: str) -> int:
    required = int(_API.get_short_path(value, None, 0))
    if required == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if required > _MAX_LOCAL_PATH_CHARACTERS + 1:
        raise ValueError(_SHORT_PATH_RANGE)
    return required


def _short_path_text(value: str, required: int) -> str:
    buffer = ctypes.create_unicode_buffer(required)
    written = int(_API.get_short_path(value, buffer, required))
    if written == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if written >= required or written > _MAX_LOCAL_PATH_CHARACTERS:
        raise ValueError(_SHORT_PATH_RANGE)
    return buffer.value


_INVALID_COMPONENT = "Windows relative name must be one normalized path component"


def _component(name: str) -> str:
    encoded = _utf16_component(name)
    if not isinstance(name, str) or _invalid_component(name, encoded):
        raise ValueError(_INVALID_COMPONENT)
    return name


def _utf16_component(name: object) -> bytes:
    if not isinstance(name, str):
        return b""
    try:
        return name.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise ValueError(_INVALID_COMPONENT) from exc


def _invalid_component(name: str, encoded: bytes) -> bool:
    if not name or name in {".", ".."}:
        return True
    return _forbidden_component_characters(name) or _unportable_component(name, encoded)


def _forbidden_component_characters(name: str) -> bool:
    if "/" in name or "\\" in name or "\x00" in name:
        return True
    return any(_reserved_or_control(character) for character in name)


def _reserved_or_control(character: str) -> bool:
    return character in '<>:"|?*' or ord(character) < 32


def _unportable_component(name: str, encoded: bytes) -> bool:
    if name[-1] in {".", " "} or name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        return True
    return len(encoded) > 65534 or unicodedata.normalize("NFC", name) != name


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
    actual_directory = _checked_kind(_attributes(handle), directory)
    information = _FileIdInfo()
    if not _API.get_information(
        handle, 18, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    file_id = bytes(information.file_id.identifier)
    if not any(file_id):
        raise OSError("Windows stable FILE_ID_INFO identity is unavailable")
    return int(information.volume_serial_number), file_id, actual_directory


def _checked_kind(attributes: int, directory: bool | None) -> bool:
    """Whether the object is a directory, refusing reparse points and the wrong kind."""
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("Windows workspace component is a reparse point")
    actual_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if directory is not None and actual_directory != directory:
        raise PermissionError("Windows workspace component has the wrong kind")
    return actual_directory


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
    parent: int,
    name: str,
    *,
    directory: bool,
    create: bool,
    writable: bool = False,
    deletable: bool = False,
    share_access: int | None = None,
) -> int:
    require_capability()
    name_buffer, unicode_name, attributes = _object_attributes(parent, name)
    handle = wintypes.HANDLE()
    io_status = _IoStatusBlock()
    desired = (
        _SYNCHRONIZE
        | _FILE_READ_ATTRIBUTES
        | _delete_access(directory, create, deletable)
        | _read_access(directory)
        | _write_access(create, writable)
    )
    share = _share_mode(directory, share_access)
    status = _open_or_create(
        handle, desired, attributes, io_status, share, _open_options(directory), create=create
    )
    if status < 0:
        raise _open_failure(int(_API.status_to_error(status)), name, create)
    return _checked_handle(int(handle.value), directory)


def _delete_access(directory: bool, create: bool, deletable: bool) -> int:
    if (create and not directory) or deletable:
        return _DELETE
    return 0


def _read_access(directory: bool) -> int:
    if directory:
        return _FILE_LIST_DIRECTORY
    return _FILE_READ_DATA


def _write_access(create: bool, writable: bool) -> int:
    if writable:
        return _FILE_WRITE_ATTRIBUTES | _FILE_WRITE_DATA
    if create:
        return _FILE_WRITE_ATTRIBUTES
    return 0


def _open_options(directory: bool) -> int:
    kind = _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    return _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT | kind


def _share_mode(directory: bool, share_access: int | None) -> int:
    if share_access is not None:
        return share_access
    if directory:
        return _FILE_SHARE_READ | _FILE_SHARE_WRITE
    return 0


def _open_or_create(handle, desired: int, attributes, io_status, share: int, options: int, *, create: bool) -> int:
    if create:
        allocation = ctypes.c_longlong(0)
        return _API.nt_create_file(
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
    return _API.nt_open_file(
        ctypes.byref(handle),
        desired,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        share,
        options,
    )


def _open_failure(error: int, name: str, create: bool) -> OSError:
    message = f"cannot {'create' if create else 'open'} Windows component: {name}"
    if error in {80, 183}:  # ERROR_FILE_EXISTS | ERROR_ALREADY_EXISTS
        return FileExistsError(error, message)
    if error in {2, 3}:  # ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND
        return FileNotFoundError(error, message)
    return OSError(error, message)


def _checked_handle(value: int, directory: bool) -> int:
    try:
        identity(value, directory=directory)
    except BaseException:
        close_handle(value)
        raise
    return value


def create_directory(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=True, create=True)


def create_writable_directory(parent: int, name: str) -> int:
    """Create a directory whose retained handle supports metadata flushing."""
    return _relative_handle(parent, name, directory=True, create=True, writable=True)


def open_directory(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=True, create=False)


def create_file(parent: int, name: str) -> int:
    return _relative_handle(
        parent, name, directory=False, create=True, writable=True
    )


def open_file(parent: int, name: str) -> int:
    return _relative_handle(parent, name, directory=False, create=False)


def open_shared_readonly_source_file(parent: int, name: str) -> int:
    """Open one no-follow source without blocking editor file activity."""
    return _relative_handle(
        parent,
        name,
        directory=False,
        create=False,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
    )


def _open_local_readonly_path(path: Path, share_access: int) -> int:
    require_capability()
    value, _pure = _bounded_local_absolute_path(path)
    handle = _API.create_file(
        f"\\\\?\\{value}",
        _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share_access,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    result = int(handle)
    try:
        identity(result, directory=False)
    except BaseException:
        close_handle(result)
        raise
    return result


def open_exclusive_readonly_source_file(path: Path) -> int:
    """Open one local no-follow file while denying writers and deleters."""
    return _open_local_readonly_path(path, _FILE_SHARE_READ)


def open_shared_readonly_runtime_file(path: Path) -> int:
    """Open one local no-follow runtime file without excluding live writers.

    Operational databases are open for writing by this process and by other
    local agents. Identity capture only needs the volume and the file id, so
    demanding a window in which nobody may write turns a normal second call in
    the same process into `ERROR_SHARING_VIOLATION`.
    """
    return _open_local_readonly_path(
        path, _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    )


def open_deletable_file(parent: int, name: str) -> int:
    return _relative_handle(
        parent, name, directory=False, create=False, deletable=True
    )


def open_deletable_directory(parent: int, name: str) -> int:
    return _relative_handle(
        parent, name, directory=True, create=False, deletable=True
    )


def _open_directory_path(path: Path, *, writable_leaf: bool) -> int:
    require_capability()
    _value, pure = _bounded_local_absolute_path(path)
    parts = pure.parts[1:]
    chain = _DirectoryChain(_open_volume_root(pure, writable=writable_leaf and not parts))
    try:
        chain.descend_all(parts, writable_leaf)
    except BaseException:
        chain.abandon()
        raise
    return chain.current_handle()


def _open_volume_root(pure: PureWindowsPath, *, writable: bool) -> int:
    desired = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES
    if writable:
        desired |= _FILE_WRITE_ATTRIBUTES | _FILE_WRITE_DATA | _SYNCHRONIZE
    root = _API.create_file(
        f"\\\\?\\{pure.drive}\\",
        desired,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if root == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(root)


def _close_quietly(handle: int) -> None:
    try:
        close_handle(handle)
    except BaseException:
        pass


class _DirectoryChain:
    """One open directory handle, moved down a path one no-follow component at a time."""

    def __init__(self, root: int) -> None:
        self.current: int | None = root

    def descend_all(self, parts: tuple[str, ...], writable_leaf: bool) -> None:
        identity(self.current, directory=True)
        last = len(parts) - 1
        for index, part in enumerate(parts):
            self._descend(part, writable=writable_leaf and index == last)

    def _descend(self, part: str, *, writable: bool) -> None:
        child = _relative_handle(
            self.current,
            part,
            directory=True,
            create=False,
            writable=writable,
        )
        try:
            close_handle(self.current)
        except BaseException:
            self.current = None
            _close_quietly(child)
            raise
        self.current = child

    def abandon(self) -> None:
        if self.current is not None:
            close_handle(self.current)

    def current_handle(self) -> int:
        if self.current is None:
            raise OSError("Windows absolute directory ownership was lost")
        return self.current


def open_directory_path(path: Path) -> int:
    """Open a local absolute directory one no-follow component at a time."""
    return _open_directory_path(path, writable_leaf=False)


def open_writable_directory_path(path: Path) -> int:
    """Open one local absolute directory for metadata writes and flushing."""
    return _open_directory_path(path, writable_leaf=True)


_DIRECTORY_BUFFER_BYTES = 64 * 1024


def list_directory(handle: int, *, max_entries: int) -> list[WindowsEntry]:
    require_capability()
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
        raise ValueError("max_entries must be a non-negative integer")
    entries = _enumerated_entries(handle, max_entries)
    _require_normalized_names(entries)
    return sorted(entries, key=lambda item: item.name)


def _enumerated_entries(handle: int, max_entries: int) -> list[WindowsEntry]:
    entries: list[WindowsEntry] = []
    restart = True
    while True:
        buffer = _directory_batch(handle, restart)
        if buffer is None:
            return entries
        _append_batch_entries(buffer, entries, max_entries)
        restart = False


def _directory_batch(handle: int, restart: bool):
    """The next buffer of directory records; None once enumeration is exhausted."""
    buffer = ctypes.create_string_buffer(_DIRECTORY_BUFFER_BYTES)
    information_class = 20 if restart else 19
    if _API.get_information(handle, information_class, ctypes.byref(buffer), _DIRECTORY_BUFFER_BYTES):
        return buffer
    error = ctypes.get_last_error()
    if error in {18, 38}:  # ERROR_NO_MORE_FILES | ERROR_HANDLE_EOF
        return None
    raise ctypes.WinError(error)


def _append_batch_entries(buffer, entries: list[WindowsEntry], max_entries: int) -> None:
    offset = 0
    while True:
        information = _FileIdExtdDirInfo.from_buffer(buffer, offset)
        _append_directory_entry(buffer, offset, information, entries, max_entries)
        next_offset = _next_record_offset(information, offset)
        if next_offset is None:
            return
        offset = next_offset


def _append_directory_entry(buffer, offset: int, information, entries: list[WindowsEntry], max_entries: int) -> None:
    name = _record_name(buffer, offset, information)
    if name in {".", ".."}:
        return
    entries.append(_windows_entry(name, information))
    if len(entries) > max_entries:
        raise ValueError("sealed workspace entry range exceeded")


def _record_name(buffer, offset: int, information) -> str:
    name_offset = _FileIdExtdDirInfo.file_name.offset
    name_length = int(information.file_name_length)
    if name_length <= 0 or name_length % 2 or offset + name_offset + name_length > _DIRECTORY_BUFFER_BYTES:
        raise OSError("Windows directory enumeration returned invalid data")
    return ctypes.wstring_at(
        ctypes.addressof(buffer) + offset + name_offset,
        name_length // 2,
    )


def _windows_entry(name: str, information) -> WindowsEntry:
    attributes = int(information.file_attributes)
    size = int(information.end_of_file)
    if size < 0:
        raise OSError("Windows directory enumeration returned a negative size")
    return WindowsEntry(name, _attribute_kind(attributes), bytes(information.file_id.identifier), size)


def _attribute_kind(attributes: int) -> str:
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return "link"
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        return "directory"
    return "file"


def _next_record_offset(information, offset: int) -> int | None:
    """The next record's offset in the buffer; None after the last record."""
    next_offset = int(information.next_entry_offset)
    if next_offset == 0:
        return None
    if next_offset < _FileIdExtdDirInfo.file_name.offset or offset + next_offset >= _DIRECTORY_BUFFER_BYTES:
        raise OSError("Windows directory enumeration returned invalid offsets")
    return offset + next_offset


def _require_normalized_names(entries: list[WindowsEntry]) -> None:
    normalized: dict[str, str] = {}
    for entry in entries:
        _claim_normalized_name(entry.name, normalized)


def _claim_normalized_name(name: str, normalized: dict[str, str]) -> None:
    if unicodedata.normalize("NFC", name) != name:
        raise PermissionError("Windows workspace contains a non-normalized name")
    folded = name.casefold()
    previous = normalized.get(folded)
    if previous is not None and previous != name:
        raise PermissionError("Windows workspace contains a case-fold collision")
    normalized[folded] = name


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
        chunk = _read_chunk(handle, chunk_bytes)
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("sealed workspace file exceeded captured range")
        yield chunk


def _read_chunk(handle: int, chunk_bytes: int) -> bytes:
    """The next chunk of the file; empty at its end."""
    buffer = ctypes.create_string_buffer(chunk_bytes)
    read = wintypes.DWORD()
    if _API.read_file(handle, buffer, chunk_bytes, ctypes.byref(read), None):
        return buffer.raw[: read.value]
    error = ctypes.get_last_error()
    if error == 38:  # ERROR_HANDLE_EOF
        return b""
    raise ctypes.WinError(error)


def seek_start(handle: int) -> None:
    require_capability()
    if not _API.set_file_pointer(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())


def file_size(handle: int) -> int:
    information = _FileStandardInfo()
    if not _API.get_information(
        handle, 1, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.directory:
        raise PermissionError("Windows workspace file became a directory")
    return int(information.end_of_file)


def file_modified_time_ns(handle: int) -> int:
    information = _FileBasicInfo()
    if not _API.get_information(
        handle, 0, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    windows_epoch_ticks = 116_444_736_000_000_000
    return max(0, (int(information.last_write_time) - windows_epoch_ticks) * 100)


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


def flush_file_path(path: Path) -> None:
    """Flush one bounded local regular file through a checked Win32 handle."""
    require_capability()
    value, _pure = _bounded_local_absolute_path(path)
    handle = _API.create_file(
        f"\\\\?\\{value}",
        _GENERIC_WRITE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    opened = int(handle)
    try:
        identity(opened, directory=False)
        flush_file(opened)
    finally:
        close_handle(opened)


def flush_directory(handle: int) -> bool:
    if _API.flush_file_buffers(handle):
        return True
    return False


def move_file_write_through(
    source: Path,
    destination: Path,
    *,
    replace: bool,
) -> None:
    """Move one local name and wait for the checked Win32 move to flush."""
    require_capability()
    source_value, _source_pure = _bounded_local_absolute_path(source)
    destination_value, _destination_pure = _bounded_local_absolute_path(destination)
    flags = _MOVEFILE_WRITE_THROUGH | (_MOVEFILE_REPLACE_EXISTING if replace else 0)
    if _API.move_file_ex(
        f"\\\\?\\{source_value}",
        f"\\\\?\\{destination_value}",
        flags,
    ):
        return
    raise _move_failure(ctypes.get_last_error(), replace)


def _move_failure(error: int, replace: bool) -> OSError:
    if not replace and error in {80, 183}:
        return FileExistsError(error, "Windows publication destination already exists")
    return ctypes.WinError(error)


def _rename_file(handle: int, parent: int, name: str, *, replace: bool) -> None:
    normalized = _component(name)
    encoded = normalized.encode("utf-16-le")
    size = _FileRenameInfo.file_name.offset + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    information = _FileRenameInfo.from_buffer(buffer)
    information.replace_if_exists = replace
    information.root_directory = parent
    information.file_name_length = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenameInfo.file_name.offset,
        encoded,
        len(encoded),
    )
    io_status = _IoStatusBlock()
    status = _API.nt_set_information_file(
        handle, ctypes.byref(io_status), ctypes.byref(buffer), size, 10
    )
    if status < 0:
        error = int(_API.status_to_error(status))
        if not replace and error in {80, 183}:
            raise FileExistsError(error, f"Windows component already exists: {name}")
        action = "replace" if replace else "publish"
        raise OSError(error, f"could not {action} Windows file")


def replace_file(handle: int, parent: int, name: str) -> None:
    """Atomically replace one file relative to its retained parent handle."""
    _rename_file(handle, parent, name, replace=True)


def publish_file(handle: int, parent: int, name: str) -> None:
    """Atomically publish one held file without replacing an existing name."""
    _rename_file(handle, parent, name, replace=False)


def delete_handle(handle: int) -> None:
    """Mark one held file or empty directory for deletion by identity."""
    information = _FileDispositionInfo(True)
    if not _API.set_information(
        handle, 4, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


__all__ = [
    "WindowsEntry",
    "capability",
    "close_handle",
    "create_directory",
    "create_file",
    "delete_handle",
    "file_modified_time_ns",
    "file_size",
    "flush_directory",
    "flush_file",
    "flush_file_path",
    "get_short_path",
    "identity",
    "is_read_only",
    "list_directory",
    "move_file_write_through",
    "open_directory",
    "open_deletable_directory",
    "open_deletable_file",
    "open_directory_path",
    "open_exclusive_readonly_source_file",
    "open_file",
    "open_shared_readonly_runtime_file",
    "open_shared_readonly_source_file",
    "open_writable_directory_path",
    "publish_file",
    "read_chunks",
    "replace_file",
    "require_capability",
    "set_read_only",
    "seek_start",
    "write_all",
]
