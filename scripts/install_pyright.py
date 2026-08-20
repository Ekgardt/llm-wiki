"""Explicit, bounded installation of the pinned managed Pyright release."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import gzip
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import stat
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import pyright_profile as _profile
import windows_workspace as _windows_workspace
from lsp_paths import managed_pyright_root
from operational_ownership import process_start_identity as _process_start_identity
from reliable_memory import (
    _set_owner_only,
    _sqlite_lock_probe,
    canonical_json_bytes,
)

DEFAULT_INSTALL_TIMEOUT_SECONDS = 120.0
NETWORK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.01
LOCK_INITIALIZATION_GRACE_SECONDS = 10.0
COPY_CHUNK_BYTES = 64 * 1024
# Registry metadata reports 5,423 files and 19,284,989 unpacked bytes.
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 8192
MAX_TOTAL_FILE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_COMPONENTS = 64
MAX_PAX_BYTES = 1024 * 1024
MAX_PAX_FIELDS = 256
MAX_EXTENDED_METADATA_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_PARENT_ENTRIES = 16_384
MAX_MOUNT_TABLE_BYTES = 4 * 1024 * 1024
MAX_LOCK_BYTES = 1024

_DARWIN_MNT_LOCAL = 0x00001000
_CLOUD_PATH_COMPONENTS = frozenset(
    {"dropbox", "googledrive", "google drive", "iclouddrive", "onedrive"}
)
_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "ceph",
        "cifs",
        "davfs",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
        "webdav",
    }
)

_LOCK_NAME = ".install-pyright-lock"
_SCRATCH_PREFIX = ".install-pyright-"
_WINDOWS_RESERVED = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class _DarwinFsid(ctypes.Structure):
    _fields_ = (("values", ctypes.c_int32 * 2),)


class _DarwinStatfs(ctypes.Structure):
    _fields_ = (
        ("block_size", ctypes.c_uint32),
        ("io_size", ctypes.c_int32),
        ("blocks", ctypes.c_uint64),
        ("blocks_free", ctypes.c_uint64),
        ("blocks_available", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("files_free", ctypes.c_uint64),
        ("filesystem_id", _DarwinFsid),
        ("owner", ctypes.c_uint32),
        ("filesystem_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("filesystem_subtype", ctypes.c_uint32),
        ("type_name", ctypes.c_char * 16),
        ("mounted_on", ctypes.c_char * 1024),
        ("mounted_from", ctypes.c_char * 1024),
        ("reserved", ctypes.c_uint32 * 8),
    )


@dataclass(frozen=True, slots=True)
class InstalledPyright:
    root: Path
    version: str
    package_sha256: str
    package_integrity: str
    server_sha256: str
    manifest_sha256: str


class PyrightInstallError(RuntimeError):
    """Stable failure from an explicit Pyright installation attempt."""

    __slots__ = ("code",)

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(slots=True)
class _Handle:
    value: int
    directory: bool
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if os.name == "nt":
            _windows_workspace.close_handle(self.value)
        else:
            os.close(self.value)


def _close_handles_preserving_error(
    primary_error: BaseException,
    *handles: _Handle | None,
) -> None:
    cleanup_errors: list[BaseException] = []
    seen: set[int] = set()
    for handle in handles:
        if handle is None or id(handle) in seen:
            continue
        seen.add(id(handle))
        try:
            handle.close()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
    if not cleanup_errors:
        return

    cause = primary_error.__cause__
    for cleanup_error in reversed(cleanup_errors):
        cleanup_error.__cause__ = cause
        cause = cleanup_error
    raise primary_error from cause


@dataclass(slots=True)
class _OwnedFile:
    parent: _Handle
    name: str
    handle: _Handle
    identity: tuple[object, ...]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.handle.close()
            self.closed = True

    def cleanup(self) -> None:
        self.close()
        _remove_owned_file(self.parent, self.name, self.identity)


@dataclass(slots=True)
class _OwnedLock:
    parent: _Handle
    name: str
    handle: _Handle
    identity: tuple[object, ...]
    nonce: str
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.handle.close()
            self.closed = True

    def cleanup(self) -> None:
        _release_owned_lock(self)


@dataclass(frozen=True, slots=True)
class _ExistingEntry:
    name: str
    kind: str
    file_id: bytes | None = None
    size: int = 0
    identity: tuple[object, ...] | None = None


class _Stage:
    def __init__(self, parent: _Handle, name: str, root: _Handle) -> None:
        self.parent = parent
        self.name = name
        self.root = root
        self.root_identity = _identity(root)
        self.directories: dict[tuple[str, ...], tuple[object, ...]] = {
            (): self.root_identity
        }
        self.published = False

    def _open_directory(
        self,
        parts: tuple[str, ...],
        *,
        writable_leaf: bool = False,
    ) -> tuple[_Handle, list[_Handle]]:
        current = self.root
        opened: list[_Handle] = []
        current_parts: tuple[str, ...] = ()
        try:
            for index, part in enumerate(parts):
                current_parts = (*current_parts, part)
                expected = self.directories.get(current_parts)
                if expected is None:
                    raise PyrightInstallError(
                        "pyright_stage_unsafe", "stage directory was not installer-created"
                    )
                child = _open_child_directory(
                    current,
                    part,
                    writable=writable_leaf and index == len(parts) - 1,
                )
                if _identity(child) != expected:
                    child.close()
                    raise PyrightInstallError(
                        "pyright_stage_unsafe", "stage directory identity changed"
                    )
                opened.append(child)
                current = child
            return current, opened
        except BaseException:
            for handle in reversed(opened):
                handle.close()
            raise

    def ensure_directory(self, parts: tuple[str, ...], deadline: float) -> None:
        current = self.root
        opened: list[_Handle] = []
        current_parts: tuple[str, ...] = ()
        try:
            for part in parts:
                _check_deadline(deadline)
                current_parts = (*current_parts, part)
                expected = self.directories.get(current_parts)
                if expected is None:
                    if _entry_kind(current, part) is not None:
                        raise PyrightInstallError(
                            "pyright_stage_unsafe", "unexpected stage entry appeared"
                        )
                    child = _create_child_directory(current, part)
                    expected = _identity(child)
                    self.directories[current_parts] = expected
                else:
                    child = _open_child_directory(current, part)
                    if _identity(child) != expected:
                        child.close()
                        raise PyrightInstallError(
                            "pyright_stage_unsafe", "stage directory identity changed"
                        )
                opened.append(child)
                current = child
        finally:
            for handle in reversed(opened):
                handle.close()

    def create_file(self, parts: tuple[str, ...], deadline: float) -> _Handle:
        if not parts:
            raise PyrightInstallError("pyright_stage_unsafe")
        self.ensure_directory(parts[:-1], deadline)
        parent, opened = self._open_directory(parts[:-1])
        try:
            _check_deadline(deadline)
            if _entry_kind(parent, parts[-1]) is not None:
                raise PyrightInstallError(
                    "pyright_stage_unsafe", "stage file name was not create-only"
                )
            return _create_child_file(parent, parts[-1])
        finally:
            for handle in reversed(opened):
                handle.close()

    def write_bytes(self, parts: tuple[str, ...], content: bytes, deadline: float) -> None:
        handle = self.create_file(parts, deadline)
        try:
            _write_handle(handle, content, deadline)
            _check_deadline(deadline)
            _checked_fsync_file(handle)
        finally:
            handle.close()

    def sync_directories(self, deadline: float) -> None:
        for parts in sorted(self.directories, key=lambda value: (-len(value), value)):
            _check_deadline(deadline)
            if not parts:
                _checked_fsync_directory(self.root)
                continue
            directory, opened = self._open_directory(parts, writable_leaf=True)
            try:
                _checked_fsync_directory(directory)
            finally:
                for handle in reversed(opened):
                    handle.close()

    def cleanup(self) -> None:
        if self.published:
            self.root.close()
            return

        def owns_staging_name() -> bool:
            try:
                if os.name == "nt":
                    volume = _identity(self.parent)[0]
                    for entry in _windows_workspace.list_directory(
                        self.parent.value,
                        max_entries=MAX_RUNTIME_PARENT_ENTRIES,
                    ):
                        if entry.name == self.name:
                            return (
                                entry.kind == "directory"
                                and (volume, entry.file_id, True) == self.root_identity
                            )
                        if entry.name.casefold() == self.name.casefold():
                            return False
                    return False
                return (
                    _named_known_identity(
                        self.parent,
                        self.name,
                        directory=True,
                    )
                    == self.root_identity
                )
            except (FileNotFoundError, OSError, RuntimeError):
                return False

        try:
            if not owns_staging_name():
                self.published = True
                return
            _clear_directory(self.root)
            if os.name == "nt":
                if owns_staging_name():
                    _windows_workspace.delete_handle(self.root.value)
                else:
                    self.published = True
            elif owns_staging_name():
                os.rmdir(self.name, dir_fd=self.parent.value)
        except (FileNotFoundError, OSError, RuntimeError):
            pass
        finally:
            self.root.close()


class _BoundedDecompressedReader(io.RawIOBase):
    def __init__(self, source: gzip.GzipFile, deadline: float) -> None:
        self._source = source
        self._deadline = deadline
        self._total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        _check_deadline(self._deadline)
        maximum = len(buffer)
        remaining = MAX_DECOMPRESSED_BYTES - self._total
        request = min(maximum, remaining + 1)
        try:
            content = self._source.read(request)
        except gzip.BadGzipFile:
            raise
        if not isinstance(content, bytes):
            raise PyrightInstallError("pyright_archive_malformed")
        self._total += len(content)
        if self._total > MAX_DECOMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_decompressed_limit")
        buffer[: len(content)] = content
        return len(content)


class _HandleReader(io.RawIOBase):
    def __init__(self, handle: _Handle) -> None:
        self._handle = handle

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        content = _read_handle(self._handle, len(buffer))
        buffer[: len(content)] = content
        return len(content)


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        raise urllib.error.HTTPError(
            request.full_url, code, "redirect refused", headers, file_pointer
        )


def _open_pinned_url(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> object:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirect(),
        urllib.request.HTTPSHandler(),
    )
    return opener.open(request, timeout=timeout)


def _validated_deadline(deadline: float | None) -> float:
    if deadline is None:
        return time.monotonic() + DEFAULT_INSTALL_TIMEOUT_SECONDS
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    return float(deadline)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Pyright installation deadline expired")


def _is_reparse_or_link(info: object) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_absolute_local(path: Path, label: str) -> None:
    raw = os.fspath(path)
    if (
        not path.is_absolute()
        or not raw
        or "\0" in raw
        or raw.startswith(("\\\\", "//"))
        or ".." in path.parts
    ):
        raise ValueError(f"{label} must be an absolute local path")


def _decode_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _path_is_under(path: str, mount_point: str) -> bool:
    normalized = mount_point.rstrip("/") or "/"
    return normalized == "/" or path == normalized or path.startswith(normalized + "/")


def _read_linux_mount_table(deadline: float) -> tuple[str, bool]:
    for path, mountinfo in (
        (Path("/proc/self/mountinfo"), True),
        (Path("/proc/mounts"), False),
    ):
        _check_deadline(deadline)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_MOUNT_TABLE_BYTES + 1)
        except TimeoutError:
            raise
        except OSError:
            continue
        _check_deadline(deadline)
        if len(raw) > MAX_MOUNT_TABLE_BYTES:
            raise OSError("Linux mount table exceeded the bounded range")
        try:
            return raw.decode("utf-8", errors="strict"), mountinfo
        except UnicodeError as exc:
            raise OSError("Linux mount table was not valid UTF-8") from exc
    raise OSError("Linux mount table was unavailable")


def _linux_filesystem_details(path: Path, deadline: float) -> tuple[str, str]:
    data, mountinfo = _read_linux_mount_table(deadline)
    target = str(path.resolve(strict=False)).replace("\\", "/")
    matches: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(data.splitlines()):
        _check_deadline(deadline)
        fields = line.split()
        try:
            if mountinfo:
                separator = fields.index("-")
                mount_point = fields[4]
                filesystem = fields[separator + 1]
                source = fields[separator + 2]
            else:
                source, mount_point, filesystem = fields[:3]
        except (IndexError, ValueError) as exc:
            raise OSError("Linux mount table was malformed") from exc
        decoded_mount = _decode_mount_field(mount_point)
        if _path_is_under(target, decoded_mount):
            matches.append(
                (
                    len(decoded_mount.rstrip("/") or "/"),
                    index,
                    filesystem,
                    _decode_mount_field(source),
                )
            )
    if not matches:
        raise OSError("Linux mount table did not identify the target filesystem")
    _length, _index, filesystem, source = max(matches)
    return filesystem, source


def _nearest_existing_path(path: Path, deadline: float) -> Path:
    for candidate in (path, *path.parents):
        _check_deadline(deadline)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        return candidate
    raise OSError("no existing filesystem ancestor was available")


def _darwin_filesystem_details(path: Path, deadline: float) -> tuple[str, str, int]:
    target = _nearest_existing_path(path, deadline)
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "statfs", None)
    if function is None:
        raise OSError("Darwin statfs is unavailable")
    function.argtypes = (ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs))
    function.restype = ctypes.c_int
    information = _DarwinStatfs()
    _check_deadline(deadline)
    if function(os.fsencode(target), ctypes.byref(information)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "Darwin statfs failed")
    _check_deadline(deadline)
    try:
        filesystem = bytes(information.type_name).split(b"\0", 1)[0].decode("ascii")
        source = bytes(information.mounted_from).split(b"\0", 1)[0].decode("utf-8")
    except UnicodeError as exc:
        raise OSError("Darwin statfs returned invalid text") from exc
    if not filesystem or not source:
        raise OSError("Darwin statfs returned incomplete filesystem identity")
    return filesystem, source, int(information.flags)


def _filesystem_type_is_network(filesystem: str) -> bool:
    normalized = filesystem.casefold()
    return normalized in _NETWORK_FILESYSTEMS or normalized.endswith(".sshfs")


def _require_local_filesystem(path: Path, deadline: float) -> None:
    _check_deadline(deadline)
    raw = os.fspath(path)
    if raw.startswith(("\\\\", "//")):
        raise PermissionError("UNC paths are not local filesystems")
    if sys.platform == "darwin":
        filesystem, source, flags = _darwin_filesystem_details(path, deadline)
        local = bool(flags & _DARWIN_MNT_LOCAL)
    elif sys.platform.startswith("linux"):
        filesystem, source = _linux_filesystem_details(path, deadline)
        local = True
    elif os.name == "nt":
        anchor = path.resolve(strict=False).anchor
        if not anchor:
            raise OSError("Windows path has no local drive anchor")
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(anchor))
        if drive_type in {0, 1}:
            raise OSError("Windows drive type is unavailable")
        filesystem = "windows-network" if drive_type == 4 else "windows-local"
        source = anchor
        local = drive_type in {2, 3, 5, 6}
    else:
        raise OSError("platform filesystem locality check is unavailable")
    _check_deadline(deadline)
    if (
        not local
        or _filesystem_type_is_network(filesystem)
        or source.startswith(("\\\\", "//"))
    ):
        raise PermissionError("path is on a network filesystem")


def _validate_existing_directory_chain(path: Path, deadline: float) -> None:
    candidates = [path, *path.parents]
    for candidate in reversed(candidates):
        _check_deadline(deadline)
        if candidate == Path(candidate.anchor):
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError("runtime path contains a link, reparse point, or non-directory")


def _validate_installer_state_root(path: Path, deadline: float) -> None:
    _check_deadline(deadline)
    if any(part.casefold() in _CLOUD_PATH_COMPONENTS for part in path.parts):
        raise PermissionError("state root is cloud-synchronized")
    _require_local_filesystem(path, deadline)
    _check_deadline(deadline)
    path.mkdir(parents=True, exist_ok=True)
    _check_deadline(deadline)
    _set_owner_only(path, 0o700)
    _check_deadline(deadline)
    lock_supported = _sqlite_lock_probe(path, deadline=deadline)
    _check_deadline(deadline)
    if lock_supported is not True:
        raise PermissionError("state root failed the SQLite locking probe")


def _prepare_state_root(state_root: Path, deadline: float) -> None:
    _check_deadline(deadline)
    _require_absolute_local(state_root, "state_root")
    try:
        _validate_existing_directory_chain(state_root, deadline)
        _check_deadline(deadline)
        _validate_installer_state_root(state_root, deadline)
        _check_deadline(deadline)
        _validate_existing_directory_chain(
            state_root / "cache/code-tools/pyright",
            deadline,
        )
        _require_local_filesystem(
            state_root / "cache/code-tools/pyright",
            deadline,
        )
        _check_deadline(deadline)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PyrightInstallError("pyright_state_root_unsafe") from exc


def _posix_directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        raise PyrightInstallError("pyright_filesystem_unsupported")
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise PyrightInstallError("pyright_filesystem_unsupported")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_absolute_directory(path: Path, *, writable: bool = False) -> _Handle:
    if os.name == "nt":
        function = (
            _windows_workspace.open_writable_directory_path
            if writable
            else _windows_workspace.open_directory_path
        )
        return _Handle(function(path), True)
    flags = _posix_directory_flags()
    absolute = path.absolute()
    current: int | None = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        if current is None:
            raise OSError("directory ownership was lost")
        return _Handle(current, True)
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def _identity(handle: _Handle) -> tuple[object, ...]:
    if handle.closed:
        raise OSError("handle is closed")
    if os.name == "nt":
        return tuple(
            _windows_workspace.identity(handle.value, directory=handle.directory)
        )
    info = os.fstat(handle.value)
    expected = stat.S_ISDIR(info.st_mode) if handle.directory else stat.S_ISREG(info.st_mode)
    if not expected:
        raise PermissionError("filesystem object changed type")
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _entry_kind(parent: _Handle, name: str) -> str | None:
    if os.name == "nt":
        for entry in _windows_workspace.list_directory(
            parent.value, max_entries=MAX_RUNTIME_PARENT_ENTRIES
        ):
            if entry.name == name:
                return entry.kind
            if entry.name.casefold() == name.casefold():
                raise PermissionError("runtime name has a case-insensitive collision")
        return None
    try:
        info = os.stat(name, dir_fd=parent.value, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _open_child_directory(
    parent: _Handle,
    name: str,
    *,
    writable: bool = False,
) -> _Handle:
    if _entry_kind(parent, name) != "directory":
        raise PermissionError("expected a regular directory")
    if os.name == "nt":
        if writable:
            return _Handle(
                _windows_workspace._relative_handle(
                    parent.value,
                    name,
                    directory=True,
                    create=False,
                    writable=True,
                ),
                True,
            )
        return _Handle(_windows_workspace.open_directory(parent.value, name), True)
    return _Handle(os.open(name, _posix_directory_flags(), dir_fd=parent.value), True)


def _create_child_directory(parent: _Handle, name: str) -> _Handle:
    if os.name == "nt":
        return _Handle(
            _windows_workspace.create_writable_directory(parent.value, name), True
        )
    os.mkdir(name, 0o700, dir_fd=parent.value)
    return _Handle(os.open(name, _posix_directory_flags(), dir_fd=parent.value), True)


def _create_child_file(parent: _Handle, name: str) -> _Handle:
    if os.name == "nt":
        return _Handle(_windows_workspace.create_file(parent.value, name), False)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    return _Handle(os.open(name, flags, 0o600, dir_fd=parent.value), False)


def _open_child_file(parent: _Handle, name: str) -> _Handle:
    if _entry_kind(parent, name) != "file":
        raise PermissionError("expected a regular file")
    if os.name == "nt":
        return _Handle(_windows_workspace.open_file(parent.value, name), False)
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    return _Handle(os.open(name, flags, dir_fd=parent.value), False)


def _named_identity(parent: _Handle, name: str, *, directory: bool) -> tuple[object, ...]:
    handle = (
        _open_child_directory(parent, name)
        if directory
        else _open_child_file(parent, name)
    )
    try:
        return _identity(handle)
    finally:
        handle.close()


def _open_known_child(parent: _Handle, name: str, *, directory: bool) -> _Handle:
    if os.name != "nt":
        return (
            _open_child_directory(parent, name)
            if directory
            else _open_child_file(parent, name)
        )
    function = (
        _windows_workspace.open_directory
        if directory
        else _windows_workspace.open_file
    )
    return _Handle(function(parent.value, name), directory)


def _open_expected_child(
    parent: _Handle,
    name: str,
    *,
    directory: bool,
    expected_identity: tuple[object, ...],
) -> _Handle:
    handle = _open_known_child(parent, name, directory=directory)
    try:
        if _identity(handle) != expected_identity:
            raise PermissionError("filesystem object changed identity")
        return handle
    except BaseException:
        handle.close()
        raise


def _named_known_identity(
    parent: _Handle,
    name: str,
    *,
    directory: bool,
) -> tuple[object, ...]:
    handle = _open_known_child(parent, name, directory=directory)
    try:
        return _identity(handle)
    finally:
        handle.close()


def _file_size(handle: _Handle) -> int:
    if os.name == "nt":
        return _windows_workspace.file_size(handle.value)
    return os.fstat(handle.value).st_size


def _read_handle(handle: _Handle, size: int) -> bytes:
    if size <= 0:
        return b""
    if os.name == "nt":
        chunks = _windows_workspace.read_chunks(
            handle.value, chunk_bytes=size, max_bytes=size
        )
        return next(chunks, b"")
    return os.read(handle.value, size)


def _seek_start(handle: _Handle) -> None:
    if os.name == "nt":
        _windows_workspace.seek_start(handle.value)
    else:
        os.lseek(handle.value, 0, os.SEEK_SET)


def _write_handle(handle: _Handle, content: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(content):
        _check_deadline(deadline)
        chunk = content[offset : offset + COPY_CHUNK_BYTES]
        if os.name == "nt":
            _windows_workspace.write_all(
                handle.value, chunk, chunk_bytes=COPY_CHUNK_BYTES
            )
            written = len(chunk)
        else:
            written = os.write(handle.value, chunk)
            if written <= 0:
                raise OSError("file write made no progress")
        offset += written


def _fsync_file(handle: _Handle) -> None:
    if os.name == "nt":
        _windows_workspace.flush_file(handle.value)
    else:
        os.fsync(handle.value)


def _fsync_directory(handle: _Handle) -> None:
    if os.name == "nt":
        if not _windows_workspace.flush_directory(handle.value):
            raise OSError("directory flush failed")
    else:
        os.fsync(handle.value)


def _checked_fsync_file(handle: _Handle) -> None:
    try:
        _fsync_file(handle)
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_install_fsync_failed") from exc


def _checked_fsync_directory(handle: _Handle) -> None:
    try:
        _fsync_directory(handle)
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_install_fsync_failed") from exc


def _remove_owned_file(
    parent: _Handle,
    name: str,
    expected_identity: tuple[object, ...],
) -> None:
    try:
        if _entry_kind(parent, name) != "file":
            return
        if os.name == "nt":
            handle = _Handle(
                _windows_workspace.open_deletable_file(parent.value, name), False
            )
            try:
                if _identity(handle) == expected_identity:
                    _windows_workspace.delete_handle(handle.value)
            finally:
                handle.close()
            return
        handle = _open_child_file(parent, name)
        try:
            if _identity(handle) == expected_identity:
                os.unlink(name, dir_fd=parent.value)
        finally:
            handle.close()
    except (FileNotFoundError, OSError, RuntimeError):
        return


def _clear_directory(directory: _Handle) -> None:
    if os.name == "nt":
        entries = _windows_workspace.list_directory(
            directory.value, max_entries=MAX_MEMBERS + 4
        )
        for entry in entries:
            if entry.kind == "directory":
                child = _Handle(
                    _windows_workspace.open_deletable_directory(
                        directory.value, entry.name
                    ),
                    True,
                )
                try:
                    _clear_directory(child)
                    _windows_workspace.delete_handle(child.value)
                finally:
                    child.close()
            else:
                child = _Handle(
                    _windows_workspace.open_deletable_file(directory.value, entry.name),
                    False,
                )
                try:
                    _windows_workspace.delete_handle(child.value)
                finally:
                    child.close()
        return

    with os.scandir(directory.value) as entries:
        names = tuple(entry.name for entry in entries)
    for name in names:
        info = os.stat(name, dir_fd=directory.value, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child = _Handle(
                os.open(name, _posix_directory_flags(), dir_fd=directory.value), True
            )
            try:
                _clear_directory(child)
            finally:
                child.close()
            os.rmdir(name, dir_fd=directory.value)
        else:
            os.unlink(name, dir_fd=directory.value)


def _ensure_runtime_parent(state_root: Path, deadline: float) -> _Handle:
    current = _open_absolute_directory(state_root, writable=True)
    pending: _Handle | None = None
    try:
        for component in ("cache", "code-tools", "pyright"):
            pending = None
            _check_deadline(deadline)
            kind = _entry_kind(current, component)
            if kind is None:
                try:
                    pending = _create_child_directory(current, component)
                except FileExistsError:
                    if _entry_kind(current, component) != "directory":
                        raise PermissionError(
                            "runtime hierarchy creation lost to an unsafe entry"
                        )
                    pending = _open_child_directory(
                        current,
                        component,
                        writable=True,
                    )
                else:
                    _check_deadline(deadline)
                    _checked_fsync_directory(current)
            elif kind == "directory":
                pending = _open_child_directory(current, component, writable=True)
            else:
                raise PermissionError("runtime hierarchy contains an unsafe entry")
            current.close()
            current = pending
            pending = None
        return current
    except BaseException as primary_error:
        _close_handles_preserving_error(primary_error, pending, current)
        raise


def _new_owned_file(parent: _Handle, purpose: str) -> _OwnedFile:
    for _attempt in range(32):
        name = f"{_SCRATCH_PREFIX}{purpose}-{secrets.token_hex(16)}"
        try:
            handle = _create_child_file(parent, name)
        except FileExistsError:
            continue
        return _OwnedFile(parent, name, handle, _identity(handle))
    raise PyrightInstallError("pyright_scratch_exhausted")


def _new_stage(parent: _Handle) -> _Stage:
    for _attempt in range(32):
        name = f"{_SCRATCH_PREFIX}stage-{secrets.token_hex(16)}"
        try:
            if os.name == "nt":
                root = _Handle(
                    _windows_workspace._relative_handle(
                        parent.value,
                        name,
                        directory=True,
                        create=True,
                        deletable=True,
                        writable=True,
                    ),
                    True,
                )
            else:
                root = _create_child_directory(parent, name)
        except FileExistsError:
            continue
        return _Stage(parent, name, root)
    raise PyrightInstallError("pyright_scratch_exhausted")


def _lock_handle_nonblocking(handle: _Handle) -> bool:
    if os.name == "nt":
        return True
    import fcntl

    try:
        fcntl.flock(handle.value, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _open_lock_for_reclaim(parent: _Handle) -> _Handle | None:
    try:
        if os.name == "nt":
            handle = _Handle(
                _windows_workspace.open_deletable_file(parent.value, _LOCK_NAME),
                False,
            )
        else:
            flags = (
                os.O_RDWR
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            handle = _Handle(os.open(_LOCK_NAME, flags, dir_fd=parent.value), False)
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    try:
        if not _lock_handle_nonblocking(handle):
            handle.close()
            return None
        return handle
    except BaseException:
        handle.close()
        raise


def _read_lock_bytes(handle: _Handle) -> bytes | None:
    size = _file_size(handle)
    if size < 0 or size > MAX_LOCK_BYTES:
        return None
    _seek_start(handle)
    content = bytearray()
    while len(content) < size:
        chunk = _read_handle(handle, size - len(content))
        if not chunk:
            return None
        content.extend(chunk)
    if _file_size(handle) != size:
        return None
    return bytes(content)


def _read_lock_record(handle: _Handle) -> tuple[bytes | None, dict[str, object] | None]:
    raw = _read_lock_bytes(handle)
    if raw is None:
        return None, None
    try:
        value = _strict_json_object(raw, "pyright_install_lock_unsafe")
    except PyrightInstallError:
        return raw, None
    if canonical_json_bytes(value) != raw or set(value) != {
        "acquired_at_unix_ns",
        "nonce",
        "pid",
        "process_start",
    }:
        return raw, None
    acquired = value.get("acquired_at_unix_ns")
    nonce = value.get("nonce")
    pid = value.get("pid")
    process_start = value.get("process_start")
    if (
        isinstance(acquired, bool)
        or not isinstance(acquired, int)
        or acquired <= 0
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(process_start, str)
        or not process_start
        or len(process_start.encode("utf-8", errors="strict")) > 256
    ):
        return raw, None
    return raw, value


def _lock_modified_time_ns(handle: _Handle) -> int:
    if os.name == "nt":
        return _windows_workspace.file_modified_time_ns(handle.value)
    return os.fstat(handle.value).st_mtime_ns


def _create_owned_lock(parent: _Handle, deadline: float) -> _OwnedLock | None:
    try:
        handle = _create_child_file(parent, _LOCK_NAME)
    except FileExistsError:
        return None
    identity = _identity(handle)
    nonce = secrets.token_hex(16)
    owned = _OwnedLock(parent, _LOCK_NAME, handle, identity, nonce)
    try:
        if not _lock_handle_nonblocking(handle):
            # Another owner holds a lock on the file this call had just created,
            # so the lock is theirs. Close ours and report the loss: the caller
            # waits and retries until its deadline. Treating it as a failure
            # deleted a lock this process does not own and aborted the install.
            owned.close()
            return None
        process_start = _process_start_identity(os.getpid())
        if process_start is None:
            raise OSError("current process identity was unavailable")
        metadata = canonical_json_bytes(
            {
                "acquired_at_unix_ns": time.time_ns(),
                "nonce": nonce,
                "pid": os.getpid(),
                "process_start": process_start,
            }
        )
        _write_handle(handle, metadata, deadline)
        _check_deadline(deadline)
        _checked_fsync_file(handle)
        _checked_fsync_directory(parent)
        return owned
    except BaseException:
        try:
            owned.close()
        finally:
            _remove_owned_file(parent, _LOCK_NAME, identity)
        raise


def _release_owned_lock(lock: _OwnedLock) -> None:
    handle = lock.handle if not lock.closed else _open_lock_for_reclaim(lock.parent)
    if handle is None:
        return
    removed = False
    try:
        if _identity(handle) != lock.identity:
            return
        _raw, metadata = _read_lock_record(handle)
        if metadata is None or metadata.get("nonce") != lock.nonce:
            return
        if os.name == "nt":
            _windows_workspace.delete_handle(handle.value)
        else:
            if _named_identity(lock.parent, lock.name, directory=False) != lock.identity:
                return
            os.unlink(lock.name, dir_fd=lock.parent.value)
        removed = True
    finally:
        if handle is lock.handle:
            lock.close()
        else:
            handle.close()
    if removed:
        _checked_fsync_directory(lock.parent)


def _try_reclaim_lock(parent: _Handle, deadline: float) -> _OwnedLock | None:
    stale = _open_lock_for_reclaim(parent)
    if stale is None:
        return None
    stale_identity = _identity(stale)
    quarantine: _OwnedFile | None = None
    try:
        initial_raw, metadata = _read_lock_record(stale)
        if metadata is None:
            age = max(0, time.time_ns() - _lock_modified_time_ns(stale)) / 1_000_000_000
            if age < LOCK_INITIALIZATION_GRACE_SECONDS:
                return None
        else:
            pid = metadata["pid"]
            process_start = metadata["process_start"]
            try:
                observed_start = _process_start_identity(pid)
            except OSError:
                return None
            if observed_start == process_start:
                return None
        _check_deadline(deadline)
        current_raw, current_metadata = _read_lock_record(stale)
        if (
            current_raw != initial_raw
            or current_metadata != metadata
            or _identity(stale) != stale_identity
        ):
            return None
        quarantine_name = f"{_SCRATCH_PREFIX}stale-lock-{secrets.token_hex(16)}"
        if os.name == "nt":
            _windows_workspace.publish_file(stale.value, parent.value, quarantine_name)
        else:
            if _named_identity(parent, _LOCK_NAME, directory=False) != stale_identity:
                return None
            os.rename(
                _LOCK_NAME,
                quarantine_name,
                src_dir_fd=parent.value,
                dst_dir_fd=parent.value,
            )
            if _named_identity(parent, quarantine_name, directory=False) != stale_identity:
                raise PyrightInstallError("pyright_install_lock_unsafe")
        quarantine = _OwnedFile(parent, quarantine_name, stale, stale_identity)
        _checked_fsync_directory(parent)
        return _create_owned_lock(parent, deadline)
    finally:
        if quarantine is not None:
            quarantine.cleanup()
        else:
            stale.close()


def _try_create_lock(parent: _Handle, deadline: float) -> _OwnedLock | None:
    owned = _create_owned_lock(parent, deadline)
    if owned is not None:
        return owned
    kind = _entry_kind(parent, _LOCK_NAME)
    if kind is None:
        return _create_owned_lock(parent, deadline)
    if kind != "file":
        raise PyrightInstallError("pyright_install_lock_unsafe")
    return _try_reclaim_lock(parent, deadline)


def _strict_json_object(raw: bytes, code: str) -> dict[str, object]:
    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_number(_value: str) -> object:
        raise ValueError("unsupported number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PyrightInstallError(code) from exc
    if not isinstance(value, dict):
        raise PyrightInstallError(code)
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 64 or nodes > 4096:
            raise PyrightInstallError(code)
        if item is None or isinstance(item, (bool, int, str)):
            continue
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise PyrightInstallError(code)
            stack.extend((child, depth + 1) for child in item.values())
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
            continue
        raise PyrightInstallError(code)
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PyrightInstallError(code) from exc
    return value


def _validate_package_json(raw: bytes) -> None:
    value = _strict_json_object(raw, "pyright_package_json_malformed")
    if value.get("name") != "pyright":
        raise PyrightInstallError("pyright_package_mismatch")
    if value.get("version") != _profile.PYRIGHT_VERSION:
        raise PyrightInstallError("pyright_version_mismatch")


def _read_bounded_file(
    parent: _Handle,
    name: str,
    maximum: int,
    deadline: float,
    *,
    expected_identity: tuple[object, ...] | None = None,
    expected_size: int | None = None,
) -> bytes:
    _check_deadline(deadline)
    handle = (
        _open_child_file(parent, name)
        if expected_identity is None
        else _open_expected_child(
            parent,
            name,
            directory=False,
            expected_identity=expected_identity,
        )
    )
    identity = _identity(handle)
    try:
        size = _file_size(handle)
        if (expected_size is not None and size != expected_size) or size > maximum:
            raise PyrightInstallError("pyright_existing_install_invalid")
        content = bytearray()
        while True:
            _check_deadline(deadline)
            chunk = _read_handle(handle, min(COPY_CHUNK_BYTES, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum:
                raise PyrightInstallError("pyright_existing_install_invalid")
        if _file_size(handle) != size or _identity(handle) != identity:
            raise PyrightInstallError("pyright_existing_install_invalid")
    finally:
        handle.close()
    named_identity = (
        _named_identity(parent, name, directory=False)
        if expected_identity is None
        else _named_known_identity(parent, name, directory=False)
    )
    if named_identity != identity:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return bytes(content)


def _existing_directory_entries(directory: _Handle) -> tuple[_ExistingEntry, ...]:
    if os.name == "nt":
        directory_identity = _identity(directory)
        values = []
        for entry in _windows_workspace.list_directory(
            directory.value, max_entries=MAX_MEMBERS + 2
        ):
            if (
                len(entry.file_id) != 16
                or not any(entry.file_id)
                or entry.size < 0
            ):
                raise PyrightInstallError("pyright_existing_install_invalid")
            values.append(
                _ExistingEntry(
                    entry.name,
                    entry.kind,
                    entry.file_id,
                    entry.size,
                    (
                        directory_identity[0],
                        entry.file_id,
                        entry.kind == "directory",
                    ),
                )
            )
        return tuple(values)
    values: list[_ExistingEntry] = []
    with os.scandir(directory.value) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                kind = "link"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            size = int(info.st_size)
            if size < 0:
                raise PyrightInstallError("pyright_existing_install_invalid")
            values.append(
                _ExistingEntry(
                    entry.name,
                    kind,
                    None,
                    size,
                    (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
                )
            )
    return tuple(sorted(values, key=lambda item: item.name))


def _validate_existing_tree(
    root: _Handle,
    deadline: float,
) -> dict[str, _ExistingEntry]:
    count = 0
    total_bytes = 0
    folded_paths: dict[str, str] = {}
    snapshots: dict[str, _ExistingEntry] = {}

    def visit(directory: _Handle, parts: tuple[str, ...]) -> None:
        nonlocal count, total_bytes
        _check_deadline(deadline)
        entries = _existing_directory_entries(directory)
        if not parts and {entry.name for entry in entries} != {
            "install-manifest.json",
            "package",
        }:
            raise PyrightInstallError("pyright_existing_install_invalid")
        for entry in entries:
            _check_deadline(deadline)
            name = entry.name
            kind = entry.kind
            count += 1
            if count > MAX_MEMBERS + 2:
                raise PyrightInstallError("pyright_existing_install_invalid")
            child_parts = (*parts, name)
            relative = "/".join(child_parts)
            try:
                encoded = relative.encode("utf-8", errors="strict")
                component_bytes = name.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise PyrightInstallError("pyright_existing_install_invalid") from exc
            stem = name.rstrip(" .").split(".", 1)[0].casefold()
            if (
                not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or ":" in name
                or name != name.rstrip(" .")
                or stem in _WINDOWS_RESERVED
                or unicodedata.normalize("NFC", name) != name
                or any(unicodedata.category(character).startswith("C") for character in name)
                or len(component_bytes) > MAX_PATH_COMPONENT_BYTES
                or len(encoded) > MAX_PATH_BYTES
                or len(child_parts) > MAX_PATH_COMPONENTS
            ):
                raise PyrightInstallError("pyright_existing_install_invalid")
            folded = unicodedata.normalize("NFC", relative).casefold()
            previous = folded_paths.get(folded)
            if previous is not None and previous != relative:
                raise PyrightInstallError("pyright_existing_install_invalid")
            folded_paths[folded] = relative
            if not parts:
                expected = "directory" if name == "package" else "file"
                if kind != expected:
                    raise PyrightInstallError("pyright_existing_install_invalid")
            if kind == "directory":
                if entry.identity is None:
                    raise PyrightInstallError("pyright_existing_install_invalid")
                child = _open_expected_child(
                    directory,
                    name,
                    directory=True,
                    expected_identity=entry.identity,
                )
                identity = _identity(child)
                try:
                    snapshots[relative] = entry
                    visit(child, child_parts)
                finally:
                    child.close()
                named_identity = (
                    _named_known_identity(directory, name, directory=True)
                    if os.name == "nt"
                    else _named_identity(directory, name, directory=True)
                )
                if named_identity != identity:
                    raise PyrightInstallError("pyright_existing_install_invalid")
            elif kind == "file":
                if entry.identity is None or entry.size > MAX_MEMBER_BYTES:
                    raise PyrightInstallError("pyright_existing_install_invalid")
                total_bytes += entry.size
                if total_bytes > MAX_TOTAL_FILE_BYTES + _profile.MAX_INSTALL_MANIFEST_BYTES:
                    raise PyrightInstallError("pyright_existing_install_invalid")
                if relative in {
                    "install-manifest.json",
                    "package/package.json",
                    _profile.PYRIGHT_SERVER_RELATIVE.as_posix(),
                }:
                    snapshots[relative] = entry
            else:
                raise PyrightInstallError("pyright_existing_install_invalid")

    visit(root, ())
    return snapshots


def _existing_result(
    parent: _Handle,
    root: Path,
    deadline: float,
) -> InstalledPyright | None:
    _check_deadline(deadline)
    try:
        kind = _entry_kind(parent, _profile.PYRIGHT_VERSION)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, PermissionError) as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc
    if kind is None:
        return None
    if kind != "directory":
        raise PyrightInstallError("pyright_existing_install_invalid")
    try:
        installation = _open_known_child(
            parent, _profile.PYRIGHT_VERSION, directory=True
        )
        installation_identity = _identity(installation)
        try:
            snapshots = _validate_existing_tree(installation, deadline)
            manifest_entry = snapshots.get("install-manifest.json")
            package_entry = snapshots.get("package")
            package_json_entry = snapshots.get("package/package.json")
            server_entry = snapshots.get(
                _profile.PYRIGHT_SERVER_RELATIVE.as_posix()
            )
            required = (manifest_entry, package_entry, package_json_entry, server_entry)
            if any(entry is None or entry.identity is None for entry in required):
                raise PyrightInstallError("pyright_existing_install_invalid")
            manifest_raw = _read_bounded_file(
                installation,
                "install-manifest.json",
                _profile.MAX_INSTALL_MANIFEST_BYTES,
                deadline,
                expected_identity=manifest_entry.identity,
                expected_size=manifest_entry.size,
            )
            manifest = _strict_json_object(
                manifest_raw, "pyright_existing_install_invalid"
            )
            if canonical_json_bytes(manifest) != manifest_raw:
                raise PyrightInstallError("pyright_existing_install_invalid")
            try:
                validated = _profile.validate_pyright_install_manifest(manifest)
            except ValueError as exc:
                raise PyrightInstallError("pyright_existing_install_invalid") from exc
            package = _open_expected_child(
                installation,
                "package",
                directory=True,
                expected_identity=package_entry.identity,
            )
            try:
                package_raw = _read_bounded_file(
                    package,
                    "package.json",
                    _profile.MAX_PACKAGE_JSON_BYTES,
                    deadline,
                    expected_identity=package_json_entry.identity,
                    expected_size=package_json_entry.size,
                )
                _validate_package_json(package_raw)
                server_raw = _read_bounded_file(
                    package,
                    "langserver.index.js",
                    min(MAX_MEMBER_BYTES, _profile.MAX_SERVER_BYTES),
                    deadline,
                    expected_identity=server_entry.identity,
                    expected_size=server_entry.size,
                )
            finally:
                package.close()
            if not server_raw:
                raise PyrightInstallError("pyright_existing_install_invalid")
            server_sha256 = hashlib.sha256(server_raw).hexdigest()
            if not hmac.compare_digest(server_sha256, validated["server_sha256"]):
                raise PyrightInstallError("pyright_existing_install_invalid")
            manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
            if (
                _named_known_identity(
                    parent,
                    _profile.PYRIGHT_VERSION,
                    directory=True,
                )
                != installation_identity
            ):
                raise PyrightInstallError("pyright_existing_install_invalid")
            return InstalledPyright(
                root=root,
                version=_profile.PYRIGHT_VERSION,
                package_sha256=_profile.PYRIGHT_PACKAGE_SHA256,
                package_integrity=_profile.PYRIGHT_PACKAGE_INTEGRITY,
                server_sha256=server_sha256,
                manifest_sha256=manifest_sha256,
            )
        finally:
            installation.close()
    except TimeoutError:
        raise
    except PyrightInstallError as exc:
        if exc.code == "pyright_existing_install_invalid":
            raise
        raise PyrightInstallError("pyright_existing_install_invalid") from exc
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc


def _new_hashes():
    return hashlib.sha256(), hashlib.sha512()


def _hash_handle(source: _Handle, deadline: float) -> tuple[str, bytes]:
    sha256, sha512 = _new_hashes()
    total = 0
    while True:
        _check_deadline(deadline)
        chunk = _read_handle(source, COPY_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_COMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_compressed_limit")
        sha256.update(chunk)
        sha512.update(chunk)
    return sha256.hexdigest(), sha512.digest()


def _copy_to_owned_file(
    source: _Handle,
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    sha256, sha512 = _new_hashes()
    total = 0
    while True:
        _check_deadline(deadline)
        chunk = _read_handle(source, COPY_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_COMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_compressed_limit")
        _write_handle(destination.handle, chunk, deadline)
        sha256.update(chunk)
        sha512.update(chunk)
    _check_deadline(deadline)
    _checked_fsync_file(destination.handle)
    return sha256.hexdigest(), sha512.digest()


def _posix_source_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _artifact_state(handle: _Handle) -> tuple[object, ...]:
    if os.name == "posix":
        return _posix_source_identity(os.fstat(handle.value))
    return (*_identity(handle), _file_size(handle))


def _copy_local_artifact(
    artifact: Path,
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    _check_deadline(deadline)
    _require_absolute_local(artifact, "artifact")
    try:
        _validate_existing_directory_chain(artifact.parent, deadline)
        _require_local_filesystem(artifact, deadline)
        before = artifact.lstat()
        if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode):
            raise PermissionError("artifact is not a regular local file")
        if before.st_size > MAX_COMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_compressed_limit")
        before_posix_identity = (
            _posix_source_identity(before) if os.name == "posix" else None
        )
        parent = _open_absolute_directory(artifact.parent)
        try:
            if os.name == "nt":
                source = _Handle(
                    _windows_workspace.open_exclusive_readonly_source_file(artifact),
                    False,
                )
            else:
                source = _open_child_file(parent, artifact.name)
            source_identity = _identity(source)
            try:
                if _file_size(source) != before.st_size:
                    raise PermissionError("artifact changed before open")
                if (
                    before_posix_identity is not None
                    and _posix_source_identity(os.fstat(source.value))
                    != before_posix_identity
                ):
                    raise PermissionError("artifact changed before open")
                result = _copy_to_owned_file(source, destination, deadline)
                if _identity(source) != source_identity:
                    raise PermissionError("artifact changed during copy")
            finally:
                source.close()
            after = artifact.lstat()
            if before_posix_identity is not None:
                stable = _posix_source_identity(after) == before_posix_identity
            else:
                stable = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
            if not stable or _named_identity(parent, artifact.name, directory=False) != source_identity:
                raise PermissionError("artifact changed or was replaced during copy")
            return result
        finally:
            parent.close()
    except TimeoutError:
        raise
    except PyrightInstallError:
        raise
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise PyrightInstallError("pyright_artifact_unsafe") from exc


def _response_content_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PyrightInstallError("pyright_download_response_invalid") from exc
    if value < 0:
        raise PyrightInstallError("pyright_download_response_invalid")
    return value


def _set_response_read_timeout(response: object, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Pyright installation deadline expired")
    try:
        stream = response.fp
        raw = stream.raw
        transport = raw._sock
        set_timeout = transport.settimeout
    except (AttributeError, TypeError) as exc:
        raise PyrightInstallError("pyright_download_response_invalid") from exc
    if not callable(set_timeout):
        raise PyrightInstallError("pyright_download_response_invalid")
    try:
        set_timeout(min(NETWORK_TIMEOUT_SECONDS, remaining))
    except TimeoutError:
        raise
    except (TypeError, ValueError) as exc:
        raise PyrightInstallError("pyright_download_response_invalid") from exc


def _download_artifact(
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    _check_deadline(deadline)
    request = urllib.request.Request(
        _profile.PYRIGHT_PACKAGE_URL,
        method="GET",
        headers={"Accept": "application/octet-stream"},
    )
    timeout = min(NETWORK_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Pyright installation deadline expired")
    try:
        response_context = _open_pinned_url(request, timeout=timeout)
    except TimeoutError:
        raise
    except urllib.error.HTTPError as exc:
        code = (
            "pyright_download_redirect"
            if 300 <= exc.code < 400
            else "pyright_download_failed"
        )
        try:
            exc.close()
        except Exception as close_error:
            exc.__cause__ = close_error
        raise PyrightInstallError(code) from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise PyrightInstallError("pyright_download_failed") from exc

    try:
        with response_context as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise PyrightInstallError("pyright_download_status")
            if response.geturl() != _profile.PYRIGHT_PACKAGE_URL:
                raise PyrightInstallError("pyright_download_url_drift")
            content_length = _response_content_length(response)
            if content_length is not None and content_length > MAX_COMPRESSED_BYTES:
                raise PyrightInstallError("pyright_archive_compressed_limit")
            read1 = getattr(response, "read1", None)
            if not callable(read1):
                raise PyrightInstallError("pyright_download_response_invalid")
            sha256, sha512 = _new_hashes()
            total = 0
            while content_length is None or total < content_length:
                _check_deadline(deadline)
                try:
                    _set_response_read_timeout(response, deadline)
                    chunk = read1(COPY_CHUNK_BYTES)
                except TimeoutError:
                    raise
                except OSError as exc:
                    raise PyrightInstallError("pyright_download_failed") from exc
                _check_deadline(deadline)
                if not isinstance(chunk, bytes):
                    raise PyrightInstallError("pyright_download_response_invalid")
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_COMPRESSED_BYTES:
                    raise PyrightInstallError("pyright_archive_compressed_limit")
                _write_handle(destination.handle, chunk, deadline)
                sha256.update(chunk)
                sha512.update(chunk)
            if content_length is not None and total != content_length:
                raise PyrightInstallError("pyright_download_truncated")
        _check_deadline(deadline)
        _checked_fsync_file(destination.handle)
        return sha256.hexdigest(), sha512.digest()
    except TimeoutError:
        raise
    except PyrightInstallError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_download_failed") from exc


def _verify_artifact_digests(package_sha256: str, package_sha512: bytes) -> None:
    expected_integrity = _profile.PYRIGHT_PACKAGE_INTEGRITY
    try:
        algorithm, encoded = expected_integrity.split("-", 1)
        expected_sha512 = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise PyrightInstallError("pyright_package_integrity_mismatch") from exc
    sha256_matches = hmac.compare_digest(
        package_sha256, _profile.PYRIGHT_PACKAGE_SHA256
    )
    sha512_matches = algorithm == "sha512" and hmac.compare_digest(
        package_sha512, expected_sha512
    )
    if not sha256_matches:
        raise PyrightInstallError("pyright_package_sha256_mismatch")
    if not sha512_matches:
        raise PyrightInstallError("pyright_package_integrity_mismatch")


def _pax_size(member: tarfile.TarInfo) -> int:
    headers = member.pax_headers
    if len(headers) > MAX_PAX_FIELDS:
        raise PyrightInstallError("pyright_archive_pax_limit")
    total = 0
    for key, value in headers.items():
        try:
            total += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 2
        except (AttributeError, UnicodeError) as exc:
            raise PyrightInstallError("pyright_archive_pax_limit") from exc
        if total > MAX_PAX_BYTES:
            raise PyrightInstallError("pyright_archive_pax_limit")
        if key.startswith("GNU.sparse") or key in {"SCHILY.realsize", "size"}:
            raise PyrightInstallError("pyright_archive_member_type")
    return total


def _member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    name = member.name
    if not isinstance(name, str):
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PyrightInstallError("pyright_archive_path_unsafe") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise PyrightInstallError("pyright_archive_path_limit")
    windows = PureWindowsPath(name)
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or windows.drive
        or windows.root
        or unicodedata.normalize("NFC", name) != name
        or any(unicodedata.category(character).startswith("C") for character in name)
    ):
        raise PyrightInstallError("pyright_archive_path_unsafe")
    parts = tuple(name.split("/"))
    if (
        len(parts) > MAX_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != "package"
    ):
        raise PyrightInstallError("pyright_archive_path_unsafe")
    for part in parts:
        try:
            part_size = len(part.encode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise PyrightInstallError("pyright_archive_path_unsafe") from exc
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if (
            part_size > MAX_PATH_COMPONENT_BYTES
            or part != part.rstrip(" .")
            or ":" in part
            or stem in _WINDOWS_RESERVED
        ):
            raise PyrightInstallError("pyright_archive_path_unsafe")
    return parts


class _ArchiveNames:
    def __init__(self) -> None:
        self.kinds: dict[str, str] = {}
        self.explicit: set[str] = set()
        self.folded: dict[str, str] = {}

    def add(self, parts: tuple[str, ...], kind: str) -> None:
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            expected_kind = kind if index == len(parts) else "directory"
            folded = unicodedata.normalize("NFC", path).casefold()
            previous_folded = self.folded.get(folded)
            if previous_folded is not None and previous_folded != path:
                raise PyrightInstallError("pyright_archive_name_collision")
            self.folded[folded] = path
            previous_kind = self.kinds.get(path)
            if previous_kind is not None and previous_kind != expected_kind:
                raise PyrightInstallError("pyright_archive_path_conflict")
            if index == len(parts) and path in self.explicit:
                raise PyrightInstallError("pyright_archive_duplicate_member")
            if expected_kind == "file" and any(
                existing.startswith(path + "/") for existing in self.kinds
            ):
                raise PyrightInstallError("pyright_archive_path_conflict")
            self.kinds[path] = expected_kind
        self.explicit.add("/".join(parts))


def _copy_member_data(
    stage: _Stage,
    parts: tuple[str, ...],
    source: io.BufferedReader,
    size: int,
    deadline: float,
    *,
    capture: bool,
) -> tuple[str, bytes | None]:
    destination = stage.create_file(parts, deadline)
    digest = hashlib.sha256()
    captured = bytearray() if capture else None
    remaining = size
    try:
        while remaining:
            _check_deadline(deadline)
            chunk = source.read(min(COPY_CHUNK_BYTES, remaining))
            if not isinstance(chunk, bytes) or not chunk:
                raise PyrightInstallError("pyright_archive_truncated_member")
            if len(chunk) > remaining:
                raise PyrightInstallError("pyright_archive_malformed")
            _write_handle(destination, chunk, deadline)
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise PyrightInstallError("pyright_archive_malformed")
        _check_deadline(deadline)
        _checked_fsync_file(destination)
    finally:
        destination.close()
    return digest.hexdigest(), bytes(captured) if captured is not None else None


def _extract_archive(
    parent: _Handle,
    artifact: _OwnedFile,
    deadline: float,
    accepted_state: tuple[object, ...],
    accepted_sha256: str,
    accepted_sha512: bytes,
) -> tuple[_Stage, bytes, str]:
    _check_deadline(deadline)
    stage = _new_stage(parent)
    try:
        if _artifact_state(artifact.handle) != accepted_state:
            raise PyrightInstallError("pyright_artifact_changed")
        _seek_start(artifact.handle)
        raw = io.BufferedReader(
            _HandleReader(artifact.handle), buffer_size=COPY_CHUNK_BYTES
        )
        compressed = gzip.GzipFile(fileobj=raw, mode="rb")
        bounded_raw = _BoundedDecompressedReader(compressed, deadline)
        bounded = io.BufferedReader(bounded_raw, buffer_size=COPY_CHUNK_BYTES)
        names = _ArchiveNames()
        members = 0
        aggregate = 0
        expected_offset = 0
        extended_metadata = 0
        package_json: bytes | None = None
        server_sha256: str | None = None
        try:
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    _check_deadline(deadline)
                    hidden_metadata = (
                        member.offset
                        - expected_offset
                        + member.offset_data
                        - member.offset
                        - tarfile.BLOCKSIZE
                    )
                    if hidden_metadata < 0 or hidden_metadata > MAX_PAX_BYTES:
                        raise PyrightInstallError("pyright_archive_pax_limit")
                    extended_metadata += hidden_metadata
                    if extended_metadata > MAX_EXTENDED_METADATA_BYTES:
                        raise PyrightInstallError("pyright_archive_pax_limit")
                    expected_offset = member.offset_data + (
                        (member.size + tarfile.BLOCKSIZE - 1)
                        // tarfile.BLOCKSIZE
                        * tarfile.BLOCKSIZE
                    )
                    members += 1
                    if members > MAX_MEMBERS:
                        raise PyrightInstallError(
                            "pyright_archive_member_count_limit"
                        )
                    _pax_size(member)
                    if member.sparse is not None or not (member.isdir() or member.isreg()):
                        raise PyrightInstallError("pyright_archive_member_type")
                    parts = _member_parts(member)
                    kind = "directory" if member.isdir() else "file"
                    names.add(parts, kind)
                    if member.isdir():
                        if member.size != 0:
                            raise PyrightInstallError("pyright_archive_member_type")
                        stage.ensure_directory(parts, deadline)
                        continue
                    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                        raise PyrightInstallError("pyright_archive_member_limit")
                    aggregate += member.size
                    if aggregate > MAX_TOTAL_FILE_BYTES:
                        raise PyrightInstallError("pyright_archive_aggregate_limit")
                    package_member = parts == ("package", "package.json")
                    server_member = parts == tuple(_profile.PYRIGHT_SERVER_RELATIVE.parts)
                    if package_member and member.size > _profile.MAX_PACKAGE_JSON_BYTES:
                        raise PyrightInstallError("pyright_package_json_oversized")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise PyrightInstallError("pyright_archive_malformed")
                    with extracted:
                        digest, captured = _copy_member_data(
                            stage,
                            parts,
                            extracted,
                            member.size,
                            deadline,
                            capture=package_member,
                        )
                    if package_member:
                        package_json = captured
                    if server_member:
                        server_sha256 = digest
            while True:
                _check_deadline(deadline)
                trailing = bounded.read(COPY_CHUNK_BYTES)
                if not isinstance(trailing, bytes):
                    raise PyrightInstallError("pyright_archive_malformed")
                if not trailing:
                    break
        except gzip.BadGzipFile as exc:
            raise PyrightInstallError("pyright_archive_malformed") from exc
        except (tarfile.TarError, EOFError) as exc:
            raise PyrightInstallError("pyright_archive_malformed") from exc
        finally:
            bounded.close()
            compressed.close()
            raw.close()
        if package_json is None:
            raise PyrightInstallError("pyright_package_json_missing")
        if server_sha256 is None:
            raise PyrightInstallError("pyright_server_missing")
        if names.kinds.get(_profile.PYRIGHT_SERVER_RELATIVE.as_posix()) != "file":
            raise PyrightInstallError("pyright_server_missing")
        if server_sha256 == hashlib.sha256(b"").hexdigest():
            raise PyrightInstallError("pyright_server_empty")
        _seek_start(artifact.handle)
        final_sha256, final_sha512 = _hash_handle(artifact.handle, deadline)
        if not (
            hmac.compare_digest(final_sha256, accepted_sha256)
            and hmac.compare_digest(final_sha512, accepted_sha512)
        ):
            raise PyrightInstallError("pyright_artifact_changed")
        if _artifact_state(artifact.handle) != accepted_state:
            raise PyrightInstallError("pyright_artifact_changed")
        return stage, package_json, server_sha256
    except TimeoutError:
        stage.cleanup()
        raise
    except OSError as exc:
        stage.cleanup()
        raise PyrightInstallError("pyright_archive_extract_failed") from exc
    except BaseException:
        stage.cleanup()
        raise


def _atomic_publish_noreplace(stage: _Stage, parent: _Handle, name: str) -> None:
    if os.name == "nt":
        _windows_workspace.publish_file(stage.root.value, parent.value, name)
        stage.published = True
        return
    old_name = os.fsencode(stage.name)
    new_name = os.fsencode(name)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        if function is None:
            raise PyrightInstallError("pyright_publish_unsupported")
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(parent.value, old_name, parent.value, new_name, 1)
    elif sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        if function is None:
            raise PyrightInstallError("pyright_publish_unsupported")
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(parent.value, old_name, parent.value, new_name, 0x00000004)
    else:
        raise PyrightInstallError("pyright_publish_unsupported")
    if result == 0:
        stage.published = True
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, "Pyright installation already exists")
    raise OSError(error, "could not publish Pyright installation")


def _install_under_lock(
    parent: _Handle,
    root: Path,
    artifact: Path | None,
    deadline: float,
) -> InstalledPyright:
    _check_deadline(deadline)
    temporary = _new_owned_file(parent, "download")
    stage: _Stage | None = None
    try:
        if artifact is None:
            package_sha256, package_sha512 = _download_artifact(temporary, deadline)
        else:
            package_sha256, package_sha512 = _copy_local_artifact(
                artifact, temporary, deadline
            )
        accepted_state = _artifact_state(temporary.handle)
        _verify_artifact_digests(package_sha256, package_sha512)
        _check_deadline(deadline)
        stage, package_json, server_sha256 = _extract_archive(
            parent,
            temporary,
            deadline,
            accepted_state,
            package_sha256,
            package_sha512,
        )
        _validate_package_json(package_json)
        manifest = _profile.build_pyright_install_manifest(
            server_sha256=server_sha256
        )
        manifest_bytes = canonical_json_bytes(manifest)
        stage.write_bytes(("install-manifest.json",), manifest_bytes, deadline)
        stage.sync_directories(deadline)
        _check_deadline(deadline)
        try:
            _atomic_publish_noreplace(stage, parent, _profile.PYRIGHT_VERSION)
        except FileExistsError:
            existing = _existing_result(parent, root, deadline)
            if existing is None:
                raise PyrightInstallError("pyright_publish_race")
            return existing
        except PyrightInstallError:
            raise
        except TimeoutError:
            raise
        except OSError as exc:
            raise PyrightInstallError("pyright_publish_failed") from exc
        stage.published = True
        _check_deadline(deadline)
        _checked_fsync_directory(parent)
        return InstalledPyright(
            root=root,
            version=_profile.PYRIGHT_VERSION,
            package_sha256=_profile.PYRIGHT_PACKAGE_SHA256,
            package_integrity=_profile.PYRIGHT_PACKAGE_INTEGRITY,
            server_sha256=server_sha256,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
    finally:
        if stage is not None:
            stage.cleanup()
        temporary.cleanup()


def install_pyright(
    *,
    state_root: Path,
    artifact: Path | None = None,
    deadline: float | None = None,
) -> InstalledPyright:
    """Install the pinned release after explicit invocation, or validate it."""
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    if artifact is not None and not isinstance(artifact, Path):
        raise TypeError("artifact must be a Path or None")
    effective_deadline = _validated_deadline(deadline)
    _check_deadline(effective_deadline)
    _prepare_state_root(state_root, effective_deadline)
    root = managed_pyright_root(state_root)
    try:
        parent = _ensure_runtime_parent(state_root, effective_deadline)
    except TimeoutError:
        raise
    except PyrightInstallError:
        raise
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise PyrightInstallError("pyright_state_root_unsafe") from exc
    lock: _OwnedLock | None = None
    try:
        existing = _existing_result(parent, root, effective_deadline)
        if existing is not None:
            return existing
        while lock is None:
            _check_deadline(effective_deadline)
            lock = _try_create_lock(parent, effective_deadline)
            if lock is not None:
                break
            time.sleep(min(LOCK_POLL_SECONDS, max(0.0, effective_deadline - time.monotonic())))
        existing = _existing_result(parent, root, effective_deadline)
        if existing is not None:
            return existing
        return _install_under_lock(
            parent, root, artifact, effective_deadline
        )
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_install_io_failed") from exc
    finally:
        if lock is not None:
            lock.cleanup()
        parent.close()


def _absolute_cli_path(value: str) -> Path:
    path = Path(value)
    try:
        _require_absolute_local(path, "path")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=_absolute_cli_path)
    parser.add_argument("--artifact", type=_absolute_cli_path)
    return parser.parse_args(argv)


MAX_CAUSE_DEPTH = 5


def _print_cause_chain(error: BaseException) -> None:
    """Print what actually failed, underneath the stable machine-readable code.

    `main` printed only `str(exc)`, so the pyright CI jobs on all three
    platforms failed with the single line `pyright_download_failed` and nothing
    to act on: the `HTTPError`, `URLError` or `OSError` that explains it was
    discarded with `__cause__`. The code stays the first line, so anything
    parsing it is unaffected; the explanation follows it.
    """
    cause = error.__cause__ or error.__context__
    for _ in range(MAX_CAUSE_DEPTH):
        if cause is None:
            return
        print(f"  caused by: {type(cause).__name__}: {cause}", file=sys.stderr)
        cause = cause.__cause__ or cause.__context__


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options: dict[str, object] = {"state_root": args.state_root}
    if args.artifact is not None:
        options["artifact"] = args.artifact
    try:
        result = install_pyright(**options)
    except (OSError, PyrightInstallError, TimeoutError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        _print_cause_chain(exc)
        return 1
    print(
        canonical_json_bytes(
            {
                "manifest_sha256": result.manifest_sha256,
                "package_integrity": result.package_integrity,
                "package_sha256": result.package_sha256,
                "root": str(result.root),
                "server_sha256": result.server_sha256,
                "version": result.version,
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
