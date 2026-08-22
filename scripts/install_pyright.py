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
    cleanup_errors = _closed_in_order(handles)
    if not cleanup_errors:
        return
    raise primary_error from _chained_cause(primary_error, cleanup_errors)


def _closed_in_order(handles: tuple[_Handle | None, ...]) -> list[BaseException]:
    """Close each distinct handle once, keeping whatever refuses to close."""
    cleanup_errors: list[BaseException] = []
    seen: set[int] = set()
    for handle in handles:
        if _already_closed(handle, seen):
            continue
        _collect_close_error(handle, cleanup_errors)
    return cleanup_errors


def _already_closed(handle: _Handle | None, seen: set[int]) -> bool:
    if handle is None:
        return True
    if id(handle) in seen:
        return True
    seen.add(id(handle))
    return False


def _collect_close_error(handle: _Handle, errors: list[BaseException]) -> None:
    try:
        handle.close()
    except BaseException as cleanup_error:
        errors.append(cleanup_error)


def _chained_cause(
    primary_error: BaseException, cleanup_errors: list[BaseException]
) -> BaseException | None:
    """Hang the cleanup failures off the original, newest nearest to it."""
    cause = primary_error.__cause__
    for cleanup_error in reversed(cleanup_errors):
        cleanup_error.__cause__ = cause
        cause = cleanup_error
    return cause


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
                current = self._open_known_stage_child(
                    current,
                    part,
                    current_parts,
                    writable=writable_leaf and index == len(parts) - 1,
                )
                opened.append(current)
            return current, opened
        except BaseException:
            for handle in reversed(opened):
                handle.close()
            raise

    def _open_known_stage_child(
        self,
        current: _Handle,
        part: str,
        current_parts: tuple[str, ...],
        *,
        writable: bool,
    ) -> _Handle:
        """A stage directory this installer created, still the one it created."""
        expected = self.directories.get(current_parts)
        if expected is None:
            raise PyrightInstallError(
                "pyright_stage_unsafe", "stage directory was not installer-created"
            )
        child = _open_child_directory(current, part, writable=writable)
        if _identity(child) != expected:
            child.close()
            raise PyrightInstallError(
                "pyright_stage_unsafe", "stage directory identity changed"
            )
        return child

    def ensure_directory(self, parts: tuple[str, ...], deadline: float) -> None:
        current = self.root
        opened: list[_Handle] = []
        current_parts: tuple[str, ...] = ()
        try:
            for part in parts:
                _check_deadline(deadline)
                current_parts = (*current_parts, part)
                current = self._stage_child(current, part, current_parts)
                opened.append(current)
        finally:
            for handle in reversed(opened):
                handle.close()

    def _stage_child(
        self, current: _Handle, part: str, current_parts: tuple[str, ...]
    ) -> _Handle:
        """The stage directory for this part: created once, identified ever after."""
        expected = self.directories.get(current_parts)
        if expected is None:
            return self._create_stage_child(current, part, current_parts)
        child = _open_child_directory(current, part)
        if _identity(child) != expected:
            child.close()
            raise PyrightInstallError(
                "pyright_stage_unsafe", "stage directory identity changed"
            )
        return child

    def _create_stage_child(
        self, current: _Handle, part: str, current_parts: tuple[str, ...]
    ) -> _Handle:
        if _entry_kind(current, part) is not None:
            raise PyrightInstallError(
                "pyright_stage_unsafe", "unexpected stage entry appeared"
            )
        child = _create_child_directory(current, part)
        self.directories[current_parts] = _identity(child)
        return child

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
        try:
            self._clear_and_remove()
        except (FileNotFoundError, OSError, RuntimeError):
            pass
        finally:
            self.root.close()

    def _clear_and_remove(self) -> None:
        if not self._owns_staging_name():
            self.published = True
            return
        _clear_directory(self.root)
        self._remove_staging_directory()

    def _remove_staging_directory(self) -> None:
        """Remove the staging name only while it still names this directory."""
        if os.name != "nt":
            if self._owns_staging_name():
                os.rmdir(self.name, dir_fd=self.parent.value)
            return
        if self._owns_staging_name():
            _windows_workspace.delete_handle(self.root.value)
            return
        self.published = True

    def _owns_staging_name(self) -> bool:
        try:
            return self._staging_name_is_ours()
        except (FileNotFoundError, OSError, RuntimeError):
            return False

    def _staging_name_is_ours(self) -> bool:
        if os.name != "nt":
            return (
                _named_known_identity(self.parent, self.name, directory=True)
                == self.root_identity
            )
        volume = _identity(self.parent)[0]
        for entry in _windows_workspace.list_directory(
            self.parent.value,
            max_entries=MAX_RUNTIME_PARENT_ENTRIES,
        ):
            decided = self._staging_entry_verdict(entry, volume)
            if decided is not None:
                return decided
        return False

    def _staging_entry_verdict(self, entry: object, volume: object) -> bool | None:
        """The answer this entry settles, or None when it is not the one."""
        if entry.name == self.name:
            return (
                entry.kind == "directory"
                and (volume, entry.file_id, True) == self.root_identity
            )
        if entry.name.casefold() == self.name.casefold():
            return False
        return None


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
    if not _is_absolute_local(path):
        raise ValueError(f"{label} must be an absolute local path")


def _is_absolute_local(path: Path) -> bool:
    raw = os.fspath(path)
    if not path.is_absolute() or not raw:
        return False
    if "\0" in raw or raw.startswith(("\\\\", "//")):
        return False
    return ".." not in path.parts


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
        raw = _read_mount_file(path)
        if raw is None:
            continue
        _check_deadline(deadline)
        return _decoded_mount_table(raw), mountinfo
    raise OSError("Linux mount table was unavailable")


def _read_mount_file(path: Path) -> bytes | None:
    """The bounded contents, or None when this table is simply not there."""
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_MOUNT_TABLE_BYTES + 1)
    except TimeoutError:
        raise
    except OSError:
        return None


def _decoded_mount_table(raw: bytes) -> str:
    if len(raw) > MAX_MOUNT_TABLE_BYTES:
        raise OSError("Linux mount table exceeded the bounded range")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise OSError("Linux mount table was not valid UTF-8") from exc


def _linux_filesystem_details(path: Path, deadline: float) -> tuple[str, str]:
    data, mountinfo = _read_linux_mount_table(deadline)
    target = str(path.resolve(strict=False)).replace("\\", "/")
    matches: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(data.splitlines()):
        _check_deadline(deadline)
        _collect_mount_match(matches, index, line, mountinfo, target)
    if not matches:
        raise OSError("Linux mount table did not identify the target filesystem")
    _length, _index, filesystem, source = max(matches)
    return filesystem, source


def _collect_mount_match(
    matches: list[tuple[int, int, str, str]],
    index: int,
    line: str,
    mountinfo: bool,
    target: str,
) -> None:
    """Keep this mount only when the target sits under it; longest wins later."""
    mount_point, filesystem, source = _mount_fields(line.split(), mountinfo)
    decoded_mount = _decode_mount_field(mount_point)
    if not _path_is_under(target, decoded_mount):
        return
    matches.append(
        (
            len(decoded_mount.rstrip("/") or "/"),
            index,
            filesystem,
            _decode_mount_field(source),
        )
    )


def _mount_fields(fields: list[str], mountinfo: bool) -> tuple[str, str, str]:
    try:
        if mountinfo:
            separator = fields.index("-")
            return fields[4], fields[separator + 1], fields[separator + 2]
        source, mount_point, filesystem = fields[:3]
    except (IndexError, ValueError) as exc:
        raise OSError("Linux mount table was malformed") from exc
    return mount_point, filesystem, source


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
    function = _darwin_statfs()
    information = _DarwinStatfs()
    _check_deadline(deadline)
    if function(os.fsencode(target), ctypes.byref(information)) != 0:
        raise OSError(ctypes.get_errno(), "Darwin statfs failed")
    _check_deadline(deadline)
    filesystem, source = _darwin_statfs_text(information)
    return filesystem, source, int(information.flags)


def _darwin_statfs() -> object:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "statfs", None)
    if function is None:
        raise OSError("Darwin statfs is unavailable")
    function.argtypes = (ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs))
    function.restype = ctypes.c_int
    return function


def _darwin_statfs_text(information: _DarwinStatfs) -> tuple[str, str]:
    try:
        filesystem = bytes(information.type_name).split(b"\0", 1)[0].decode("ascii")
        source = bytes(information.mounted_from).split(b"\0", 1)[0].decode("utf-8")
    except UnicodeError as exc:
        raise OSError("Darwin statfs returned invalid text") from exc
    if not filesystem or not source:
        raise OSError("Darwin statfs returned incomplete filesystem identity")
    return filesystem, source


def _filesystem_type_is_network(filesystem: str) -> bool:
    normalized = filesystem.casefold()
    return normalized in _NETWORK_FILESYSTEMS or normalized.endswith(".sshfs")


def _require_local_filesystem(path: Path, deadline: float) -> None:
    _check_deadline(deadline)
    if os.fspath(path).startswith(("\\\\", "//")):
        raise PermissionError("UNC paths are not local filesystems")
    filesystem, source, local = _filesystem_locality(path, deadline)
    _check_deadline(deadline)
    if not _is_local_filesystem(filesystem, source, local):
        raise PermissionError("path is on a network filesystem")


def _filesystem_locality(path: Path, deadline: float) -> tuple[str, str, bool]:
    """The filesystem type, its source, and whether the platform calls it local."""
    if sys.platform == "darwin":
        filesystem, source, flags = _darwin_filesystem_details(path, deadline)
        return filesystem, source, bool(flags & _DARWIN_MNT_LOCAL)
    if sys.platform.startswith("linux"):
        filesystem, source = _linux_filesystem_details(path, deadline)
        return filesystem, source, True
    if os.name == "nt":
        return _windows_filesystem_locality(path)
    raise OSError("platform filesystem locality check is unavailable")


def _windows_filesystem_locality(path: Path) -> tuple[str, str, bool]:
    anchor = path.resolve(strict=False).anchor
    if not anchor:
        raise OSError("Windows path has no local drive anchor")
    drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(anchor))
    if drive_type in {0, 1}:
        raise OSError("Windows drive type is unavailable")
    return _windows_drive_kind(drive_type), anchor, drive_type in {2, 3, 5, 6}


def _windows_drive_kind(drive_type: int) -> str:
    if drive_type == 4:
        return "windows-network"
    return "windows-local"


def _is_local_filesystem(filesystem: str, source: str, local: bool) -> bool:
    if not local:
        return False
    if _filesystem_type_is_network(filesystem):
        return False
    return not source.startswith(("\\\\", "//"))


def _validate_existing_directory_chain(path: Path, deadline: float) -> None:
    for candidate in reversed([path, *path.parents]):
        _check_deadline(deadline)
        _require_plain_directory(candidate)


def _require_plain_directory(candidate: Path) -> None:
    """An anchor and a missing component are both fine; a link is not."""
    if candidate == Path(candidate.anchor):
        return
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return
    if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError(
            "runtime path contains a link, reparse point, or non-directory"
        )


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
    _require_posix_directory_support()
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _require_posix_directory_support() -> None:
    if not _has_posix_directory_flags():
        raise PyrightInstallError("pyright_filesystem_unsupported")
    if not _has_dir_fd_support():
        raise PyrightInstallError("pyright_filesystem_unsupported")


def _has_posix_directory_flags() -> bool:
    if os.name != "posix":
        return False
    return all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))


def _has_dir_fd_support() -> bool:
    return os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd


def _open_absolute_directory(path: Path, *, writable: bool = False) -> _Handle:
    if os.name == "nt":
        return _Handle(_windows_directory_opener(writable)(path), True)
    return _open_posix_directory_chain(path)


def _windows_directory_opener(writable: bool) -> object:
    if writable:
        return _windows_workspace.open_writable_directory_path
    return _windows_workspace.open_directory_path


def _open_posix_directory_chain(path: Path) -> _Handle:
    """Descend one component at a time so no step can follow a link."""
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
        return _windows_entry_kind(parent, name)
    return _posix_entry_kind(parent, name)


def _windows_entry_kind(parent: _Handle, name: str) -> str | None:
    for entry in _windows_workspace.list_directory(
        parent.value, max_entries=MAX_RUNTIME_PARENT_ENTRIES
    ):
        kind = _matching_entry_kind(entry, name)
        if kind is not None:
            return kind
    return None


def _matching_entry_kind(entry: object, name: str) -> str | None:
    """This entry's kind when it is the name, refusal when it only folds to it."""
    if entry.name == name:
        return entry.kind
    if entry.name.casefold() == name.casefold():
        raise PermissionError("runtime name has a case-insensitive collision")
    return None


def _posix_entry_kind(parent: _Handle, name: str) -> str | None:
    try:
        info = os.stat(name, dir_fd=parent.value, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _stat_kind(info.st_mode)


def _stat_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
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


def _write_chunk(handle: _Handle, chunk: bytes) -> int:
    if os.name == "nt":
        _windows_workspace.write_all(
            handle.value, chunk, chunk_bytes=COPY_CHUNK_BYTES
        )
        return len(chunk)
    written = os.write(handle.value, chunk)
    if written <= 0:
        raise OSError("file write made no progress")
    return written


def _write_handle(handle: _Handle, content: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(content):
        _check_deadline(deadline)
        chunk = content[offset : offset + COPY_CHUNK_BYTES]
        offset += _write_chunk(handle, chunk)


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
        _delete_if_identity_matches(parent, name, expected_identity)
    except (FileNotFoundError, OSError, RuntimeError):
        return


def _delete_if_identity_matches(
    parent: _Handle, name: str, expected_identity: tuple[object, ...]
) -> None:
    """Delete the file only while the name still points at the same object."""
    if os.name == "nt":
        _delete_windows_named_file(parent, name, expected_identity)
        return
    handle = _open_child_file(parent, name)
    try:
        if _identity(handle) == expected_identity:
            os.unlink(name, dir_fd=parent.value)
    finally:
        handle.close()


def _delete_windows_named_file(
    parent: _Handle, name: str, expected_identity: tuple[object, ...]
) -> None:
    handle = _Handle(
        _windows_workspace.open_deletable_file(parent.value, name), False
    )
    try:
        if _identity(handle) == expected_identity:
            _windows_workspace.delete_handle(handle.value)
    finally:
        handle.close()


def _clear_directory(directory: _Handle) -> None:
    if os.name == "nt":
        _clear_windows_directory(directory)
        return
    _clear_posix_directory(directory)


def _clear_windows_directory(directory: _Handle) -> None:
    for entry in _windows_workspace.list_directory(
        directory.value, max_entries=MAX_MEMBERS + 4
    ):
        _delete_windows_entry(directory, entry)


def _delete_windows_entry(directory: _Handle, entry: object) -> None:
    if entry.kind == "directory":
        _delete_windows_subtree(directory, entry.name)
        return
    child = _Handle(
        _windows_workspace.open_deletable_file(directory.value, entry.name), False
    )
    try:
        _windows_workspace.delete_handle(child.value)
    finally:
        child.close()


def _delete_windows_subtree(directory: _Handle, name: str) -> None:
    child = _Handle(
        _windows_workspace.open_deletable_directory(directory.value, name), True
    )
    try:
        _clear_directory(child)
        _windows_workspace.delete_handle(child.value)
    finally:
        child.close()


def _clear_posix_directory(directory: _Handle) -> None:
    with os.scandir(directory.value) as entries:
        names = tuple(entry.name for entry in entries)
    for name in names:
        _remove_posix_entry(directory, name)


def _remove_posix_entry(directory: _Handle, name: str) -> None:
    """A real directory is emptied then removed; anything else is unlinked."""
    info = os.stat(name, dir_fd=directory.value, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=directory.value)
        return
    child = _Handle(
        os.open(name, _posix_directory_flags(), dir_fd=directory.value), True
    )
    try:
        _clear_directory(child)
    finally:
        child.close()
    os.rmdir(name, dir_fd=directory.value)


def _ensure_runtime_parent(state_root: Path, deadline: float) -> _Handle:
    current = _open_absolute_directory(state_root, writable=True)
    pending: _Handle | None = None
    try:
        for component in ("cache", "code-tools", "pyright"):
            pending, created = _runtime_child(current, component, deadline)
            if created:
                _check_deadline(deadline)
                _checked_fsync_directory(current)
            current.close()
            current = pending
            pending = None
        return current
    except BaseException as primary_error:
        _close_handles_preserving_error(primary_error, pending, current)
        raise


def _runtime_child(
    current: _Handle, component: str, deadline: float
) -> tuple[_Handle, bool]:
    """The child directory and whether this call is the one that created it.

    The handle is returned before the parent is flushed, so a flush failure
    still finds it in the caller's hands and closes it. Leaving it a local here
    leaked it, and a leaked directory handle on Windows blocks the retry.
    """
    _check_deadline(deadline)
    kind = _entry_kind(current, component)
    if kind is None:
        return _created_runtime_child(current, component)
    if kind == "directory":
        return _open_child_directory(current, component, writable=True), False
    raise PermissionError("runtime hierarchy contains an unsafe entry")


def _created_runtime_child(
    current: _Handle, component: str
) -> tuple[_Handle, bool]:
    try:
        return _create_child_directory(current, component), True
    except FileExistsError:
        return _adopted_runtime_child(current, component), False


def _adopted_runtime_child(current: _Handle, component: str) -> _Handle:
    """Lost the creation race: adopt the directory, refuse anything else."""
    if _entry_kind(current, component) != "directory":
        raise PermissionError("runtime hierarchy creation lost to an unsafe entry")
    return _open_child_directory(current, component, writable=True)


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
    content = _read_exactly(handle, size)
    if content is None or _file_size(handle) != size:
        return None
    return bytes(content)


def _read_exactly(handle: _Handle, size: int) -> bytearray | None:
    """Exactly this many bytes, or None when the file ended early."""
    content = bytearray()
    while len(content) < size:
        chunk = _read_handle(handle, size - len(content))
        if not chunk:
            return None
        content.extend(chunk)
    return content


_LOCK_RECORD_KEYS = frozenset(
    {"acquired_at_unix_ns", "nonce", "pid", "process_start"}
)


def _read_lock_record(handle: _Handle) -> tuple[bytes | None, dict[str, object] | None]:
    raw = _read_lock_bytes(handle)
    if raw is None:
        return None, None
    value = _parsed_lock_object(raw)
    if value is None or not _lock_record_is_well_formed(value):
        return raw, None
    return raw, value


def _parsed_lock_object(raw: bytes) -> dict[str, object] | None:
    """The canonical object carrying exactly the lock's keys, or None."""
    try:
        value = _strict_json_object(raw, "pyright_install_lock_unsafe")
    except PyrightInstallError:
        return None
    if canonical_json_bytes(value) != raw:
        return None
    if set(value) != _LOCK_RECORD_KEYS:
        return None
    return value


def _lock_record_is_well_formed(value: dict[str, object]) -> bool:
    return (
        _is_positive_int(value.get("acquired_at_unix_ns"))
        and _is_lock_nonce(value.get("nonce"))
        and _is_positive_int(value.get("pid"))
        and _is_process_start(value.get("process_start"))
    )


def _is_positive_int(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value > 0


def _is_lock_nonce(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _is_process_start(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return len(value.encode("utf-8", errors="strict")) <= 256


def _lock_modified_time_ns(handle: _Handle) -> int:
    if os.name == "nt":
        return _windows_workspace.file_modified_time_ns(handle.value)
    return os.fstat(handle.value).st_mtime_ns


_WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({5, 32, 33})


def _is_windows_lock_contention(exc: OSError) -> bool:
    """Whether Windows is saying the lock belongs to somebody else right now.

    ERROR_ACCESS_DENIED (5) is the answer once a file is marked for deletion;
    ERROR_SHARING_VIOLATION (32) and ERROR_LOCK_VIOLATION (33) while it is open.
    All three mean what FileExistsError means here: this call lost the race.

    A genuine permission problem is swept into the same wait and surfaces as the
    caller's timeout rather than as an IO failure. That is the price of not
    aborting an install because two of them started together.
    """
    if os.name != "nt":
        return False
    return exc.errno in _WINDOWS_LOCK_CONTENTION_ERRORS


def _created_lock_file(parent: _Handle) -> _Handle | None:
    """The newly created lock file, or None when this call lost the race."""
    try:
        return _create_child_file(parent, _LOCK_NAME)
    except FileExistsError:
        return None
    except OSError as exc:
        if not _is_windows_lock_contention(exc):
            raise
        return None


def _create_owned_lock(parent: _Handle, deadline: float) -> _OwnedLock | None:
    handle = _created_lock_file(parent)
    if handle is None:
        return None
    identity = _identity(handle)
    nonce = secrets.token_hex(16)
    owned = _OwnedLock(parent, _LOCK_NAME, handle, identity, nonce)
    try:
        return _claim_owned_lock(owned, parent, handle, nonce, deadline)
    except BaseException:
        try:
            owned.close()
        finally:
            _remove_owned_file(parent, _LOCK_NAME, identity)
        raise


def _claim_owned_lock(
    owned: _OwnedLock,
    parent: _Handle,
    handle: _Handle,
    nonce: str,
    deadline: float,
) -> _OwnedLock | None:
    if not _lock_handle_nonblocking(handle):
        # Another owner holds a lock on the file this call had just created,
        # so the lock is theirs. Close ours and report the loss: the caller
        # waits and retries until its deadline. Treating it as a failure
        # deleted a lock this process does not own and aborted the install.
        owned.close()
        return None
    _write_handle(handle, _lock_metadata_bytes(nonce), deadline)
    _check_deadline(deadline)
    _checked_fsync_file(handle)
    _checked_fsync_directory(parent)
    return owned


def _lock_metadata_bytes(nonce: str) -> bytes:
    process_start = _process_start_identity(os.getpid())
    if process_start is None:
        raise OSError("current process identity was unavailable")
    return canonical_json_bytes(
        {
            "acquired_at_unix_ns": time.time_ns(),
            "nonce": nonce,
            "pid": os.getpid(),
            "process_start": process_start,
        }
    )


def _release_owned_lock(lock: _OwnedLock) -> None:
    handle = _lock_handle_for_release(lock)
    if handle is None:
        return
    removed = False
    try:
        removed = _remove_held_lock(lock, handle)
    finally:
        _close_release_handle(lock, handle)
    if removed:
        _checked_fsync_directory(lock.parent)


def _lock_handle_for_release(lock: _OwnedLock) -> _Handle | None:
    if lock.closed:
        return _open_lock_for_reclaim(lock.parent)
    return lock.handle


def _remove_held_lock(lock: _OwnedLock, handle: _Handle) -> bool:
    """True when this call removed a lock it still owned."""
    if not _still_our_lock(lock, handle):
        return False
    if os.name == "nt":
        _windows_workspace.delete_handle(handle.value)
        return True
    if _named_identity(lock.parent, lock.name, directory=False) != lock.identity:
        return False
    os.unlink(lock.name, dir_fd=lock.parent.value)
    return True


def _still_our_lock(lock: _OwnedLock, handle: _Handle) -> bool:
    if _identity(handle) != lock.identity:
        return False
    _raw, metadata = _read_lock_record(handle)
    if metadata is None:
        return False
    return metadata.get("nonce") == lock.nonce


def _close_release_handle(lock: _OwnedLock, handle: _Handle) -> None:
    if handle is lock.handle:
        lock.close()
        return
    handle.close()


def _try_reclaim_lock(parent: _Handle, deadline: float) -> _OwnedLock | None:
    stale = _open_lock_for_reclaim(parent)
    if stale is None:
        return None
    quarantine: _OwnedFile | None = None
    try:
        quarantine = _quarantined_stale_lock(parent, stale, deadline)
        if quarantine is None:
            return None
        _checked_fsync_directory(parent)
        return _create_owned_lock(parent, deadline)
    finally:
        _close_reclaim_handles(quarantine, stale)


def _quarantined_stale_lock(
    parent: _Handle, stale: _Handle, deadline: float
) -> _OwnedFile | None:
    """Move an abandoned lock aside, or None when it is not ours to move."""
    stale_identity = _identity(stale)
    initial_raw, metadata = _read_lock_record(stale)
    if not _lock_looks_abandoned(stale, metadata):
        return None
    _check_deadline(deadline)
    if not _lock_unchanged(stale, stale_identity, initial_raw, metadata):
        return None
    quarantine_name = f"{_SCRATCH_PREFIX}stale-lock-{secrets.token_hex(16)}"
    if not _quarantine_stale_lock(parent, stale, stale_identity, quarantine_name):
        return None
    return _OwnedFile(parent, quarantine_name, stale, stale_identity)


def _close_reclaim_handles(quarantine: _OwnedFile | None, stale: _Handle) -> None:
    if quarantine is not None:
        quarantine.cleanup()
        return
    stale.close()


def _lock_looks_abandoned(
    stale: _Handle, metadata: dict[str, object] | None
) -> bool:
    """Abandoned means the writer is gone, or it never finished writing."""
    if metadata is None:
        age = max(0, time.time_ns() - _lock_modified_time_ns(stale)) / 1_000_000_000
        return age >= LOCK_INITIALIZATION_GRACE_SECONDS
    return not _lock_owner_is_alive(metadata)


def _lock_owner_is_alive(metadata: dict[str, object]) -> bool:
    """An unreadable process identity counts as alive: never reclaim on doubt."""
    try:
        observed_start = _process_start_identity(metadata["pid"])
    except OSError:
        return True
    return observed_start == metadata["process_start"]


def _lock_unchanged(
    stale: _Handle,
    stale_identity: tuple[object, ...],
    initial_raw: bytes | None,
    metadata: dict[str, object] | None,
) -> bool:
    current_raw, current_metadata = _read_lock_record(stale)
    if current_raw != initial_raw or current_metadata != metadata:
        return False
    return _identity(stale) == stale_identity


def _quarantine_stale_lock(
    parent: _Handle,
    stale: _Handle,
    stale_identity: tuple[object, ...],
    quarantine_name: str,
) -> bool:
    if os.name == "nt":
        _windows_workspace.publish_file(stale.value, parent.value, quarantine_name)
        return True
    if _named_identity(parent, _LOCK_NAME, directory=False) != stale_identity:
        return False
    os.rename(
        _LOCK_NAME,
        quarantine_name,
        src_dir_fd=parent.value,
        dst_dir_fd=parent.value,
    )
    if _named_identity(parent, quarantine_name, directory=False) != stale_identity:
        raise PyrightInstallError("pyright_install_lock_unsafe")
    return True


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


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_number(_value: str) -> object:
    raise ValueError("unsupported number")


def _loaded_strict_json(raw: bytes, code: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise PyrightInstallError(code) from exc


def _json_mapping_children(
    item: dict[object, object], depth: int, code: str
) -> list[tuple[object, int]]:
    if any(not isinstance(key, str) for key in item):
        raise PyrightInstallError(code)
    return [(child, depth + 1) for child in item.values()]


def _is_json_leaf(item: object) -> bool:
    if item is None:
        return True
    return isinstance(item, (bool, int, str))


def _json_children(item: object, depth: int, code: str) -> list[tuple[object, int]]:
    """What this node holds, refusing any type the format does not allow."""
    if _is_json_leaf(item):
        return []
    if isinstance(item, dict):
        return _json_mapping_children(item, depth, code)
    if isinstance(item, list):
        return [(child, depth + 1) for child in item]
    raise PyrightInstallError(code)


def _require_bounded_json_shape(value: dict[str, object], code: str) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 64 or nodes > 4096:
            raise PyrightInstallError(code)
        stack.extend(_json_children(item, depth, code))


def _require_canonicalizable(value: dict[str, object], code: str) -> None:
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PyrightInstallError(code) from exc


def _strict_json_object(raw: bytes, code: str) -> dict[str, object]:
    value = _loaded_strict_json(raw, code)
    if not isinstance(value, dict):
        raise PyrightInstallError(code)
    _require_bounded_json_shape(value, code)
    _require_canonicalizable(value, code)
    return value


def _validate_package_json(raw: bytes) -> None:
    value = _strict_json_object(raw, "pyright_package_json_malformed")
    if value.get("name") != "pyright":
        raise PyrightInstallError("pyright_package_mismatch")
    if value.get("version") != _profile.PYRIGHT_VERSION:
        raise PyrightInstallError("pyright_version_mismatch")


def _open_bounded_source(
    parent: _Handle, name: str, expected_identity: tuple[object, ...] | None
) -> _Handle:
    if expected_identity is None:
        return _open_child_file(parent, name)
    return _open_expected_child(
        parent, name, directory=False, expected_identity=expected_identity
    )


def _require_expected_size(
    size: int, expected_size: int | None, maximum: int
) -> None:
    if expected_size is not None and size != expected_size:
        raise PyrightInstallError("pyright_existing_install_invalid")
    if size > maximum:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _require_stable_source(
    handle: _Handle, size: int, identity: tuple[object, ...]
) -> None:
    if _file_size(handle) != size or _identity(handle) != identity:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _read_within_bounds(
    handle: _Handle,
    maximum: int,
    deadline: float,
    expected_size: int | None,
    identity: tuple[object, ...],
) -> bytearray:
    size = _file_size(handle)
    _require_expected_size(size, expected_size, maximum)
    content = bytearray()
    while True:
        _check_deadline(deadline)
        chunk = _read_handle(
            handle, min(COPY_CHUNK_BYTES, maximum + 1 - len(content))
        )
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > maximum:
            raise PyrightInstallError("pyright_existing_install_invalid")
    _require_stable_source(handle, size, identity)
    return content


def _named_bounded_identity(
    parent: _Handle, name: str, expected_identity: tuple[object, ...] | None
) -> tuple[object, ...]:
    if expected_identity is None:
        return _named_identity(parent, name, directory=False)
    return _named_known_identity(parent, name, directory=False)


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
    handle = _open_bounded_source(parent, name, expected_identity)
    identity = _identity(handle)
    try:
        content = _read_within_bounds(
            handle, maximum, deadline, expected_size, identity
        )
    finally:
        handle.close()
    if _named_bounded_identity(parent, name, expected_identity) != identity:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return bytes(content)


def _existing_directory_entries(directory: _Handle) -> tuple[_ExistingEntry, ...]:
    if os.name == "nt":
        return _windows_existing_entries(directory)
    return _posix_existing_entries(directory)


def _windows_existing_entries(directory: _Handle) -> tuple[_ExistingEntry, ...]:
    volume = _identity(directory)[0]
    return tuple(
        _windows_existing_entry(entry, volume)
        for entry in _windows_workspace.list_directory(
            directory.value, max_entries=MAX_MEMBERS + 2
        )
    )


def _windows_existing_entry(entry: object, volume: object) -> _ExistingEntry:
    _require_usable_windows_entry(entry)
    return _ExistingEntry(
        entry.name,
        entry.kind,
        entry.file_id,
        entry.size,
        (volume, entry.file_id, entry.kind == "directory"),
    )


def _require_usable_windows_entry(entry: object) -> None:
    if len(entry.file_id) != 16 or not any(entry.file_id):
        raise PyrightInstallError("pyright_existing_install_invalid")
    if entry.size < 0:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _posix_existing_entries(directory: _Handle) -> tuple[_ExistingEntry, ...]:
    with os.scandir(directory.value) as entries:
        values = [_posix_existing_entry(entry) for entry in entries]
    return tuple(sorted(values, key=lambda item: item.name))


def _posix_existing_entry(entry: os.DirEntry) -> _ExistingEntry:
    info = entry.stat(follow_symlinks=False)
    size = int(info.st_size)
    if size < 0:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return _ExistingEntry(
        entry.name,
        _stat_kind(info.st_mode),
        None,
        size,
        (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
    )


_SNAPSHOT_FILES = frozenset({"install-manifest.json", "package/package.json"})


def _snapshot_paths() -> frozenset[str]:
    return _SNAPSHOT_FILES | {_profile.PYRIGHT_SERVER_RELATIVE.as_posix()}


def _require_expected_root_entries(
    parts: tuple[str, ...], entries: tuple[_ExistingEntry, ...]
) -> None:
    if parts:
        return
    if {entry.name for entry in entries} != {"install-manifest.json", "package"}:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _encoded_member_name(name: str, relative: str) -> tuple[bytes, bytes]:
    try:
        return (
            relative.encode("utf-8", errors="strict"),
            name.encode("utf-8", errors="strict"),
        )
    except UnicodeError as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc


def _is_plain_unicode_name(name: str) -> bool:
    if unicodedata.normalize("NFC", name) != name:
        return False
    return not any(
        unicodedata.category(character).startswith("C") for character in name
    )


def _member_name_is_safe(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if any(character in name for character in ("/", "\\", ":")):
        return False
    return _member_name_is_windows_safe(name)


def _member_name_is_windows_safe(name: str) -> bool:
    """Trailing dots and spaces, and reserved device names, are Windows traps."""
    if name != name.rstrip(" ."):
        return False
    if name.rstrip(" .").split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        return False
    return _is_plain_unicode_name(name)


def _require_safe_member_name(
    name: str, child_parts: tuple[str, ...], relative: str
) -> None:
    encoded, component_bytes = _encoded_member_name(name, relative)
    if not _member_name_is_safe(name):
        raise PyrightInstallError("pyright_existing_install_invalid")
    if len(component_bytes) > MAX_PATH_COMPONENT_BYTES:
        raise PyrightInstallError("pyright_existing_install_invalid")
    if len(encoded) > MAX_PATH_BYTES:
        raise PyrightInstallError("pyright_existing_install_invalid")
    if len(child_parts) > MAX_PATH_COMPONENTS:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _require_unique_folded_path(folded_paths: dict[str, str], relative: str) -> None:
    """Two names that fold together would collide on a case-folding filesystem."""
    folded = unicodedata.normalize("NFC", relative).casefold()
    previous = folded_paths.get(folded)
    if previous is not None and previous != relative:
        raise PyrightInstallError("pyright_existing_install_invalid")
    folded_paths[folded] = relative


def _require_expected_root_kind(
    parts: tuple[str, ...], entry: _ExistingEntry
) -> None:
    if parts:
        return
    expected = "directory" if entry.name == "package" else "file"
    if entry.kind != expected:
        raise PyrightInstallError("pyright_existing_install_invalid")


def _visited_file_bytes(entry: _ExistingEntry, total_bytes: int) -> int:
    if entry.identity is None or entry.size > MAX_MEMBER_BYTES:
        raise PyrightInstallError("pyright_existing_install_invalid")
    total = total_bytes + entry.size
    if total > MAX_TOTAL_FILE_BYTES + _profile.MAX_INSTALL_MANIFEST_BYTES:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return total


def _record_interesting_file(
    snapshots: dict[str, _ExistingEntry], relative: str, entry: _ExistingEntry
) -> None:
    if relative not in _snapshot_paths():
        return
    snapshots[relative] = entry


def _child_directory_identity(directory: _Handle, name: str) -> tuple[object, ...]:
    if os.name == "nt":
        return _named_known_identity(directory, name, directory=True)
    return _named_identity(directory, name, directory=True)


def _validate_existing_tree(
    root: _Handle,
    deadline: float,
) -> dict[str, _ExistingEntry]:
    count = 0
    total_bytes = 0
    folded_paths: dict[str, str] = {}
    snapshots: dict[str, _ExistingEntry] = {}

    def visit(directory: _Handle, parts: tuple[str, ...]) -> None:
        _check_deadline(deadline)
        entries = _existing_directory_entries(directory)
        _require_expected_root_entries(parts, entries)
        for entry in entries:
            _check_deadline(deadline)
            visit_entry(directory, parts, entry)

    def visit_entry(
        directory: _Handle, parts: tuple[str, ...], entry: _ExistingEntry
    ) -> None:
        nonlocal count, total_bytes
        count += 1
        if count > MAX_MEMBERS + 2:
            raise PyrightInstallError("pyright_existing_install_invalid")
        child_parts = (*parts, entry.name)
        relative = "/".join(child_parts)
        _require_safe_member_name(entry.name, child_parts, relative)
        _require_unique_folded_path(folded_paths, relative)
        _require_expected_root_kind(parts, entry)
        if entry.kind == "directory":
            snapshots[relative] = entry
            visit_directory(directory, entry, child_parts)
            return
        if entry.kind != "file":
            raise PyrightInstallError("pyright_existing_install_invalid")
        total_bytes = _visited_file_bytes(entry, total_bytes)
        _record_interesting_file(snapshots, relative, entry)

    def visit_directory(
        directory: _Handle, entry: _ExistingEntry, child_parts: tuple[str, ...]
    ) -> None:
        if entry.identity is None:
            raise PyrightInstallError("pyright_existing_install_invalid")
        child = _open_expected_child(
            directory,
            entry.name,
            directory=True,
            expected_identity=entry.identity,
        )
        identity = _identity(child)
        try:
            visit(child, child_parts)
        finally:
            child.close()
        if _child_directory_identity(directory, entry.name) != identity:
            raise PyrightInstallError("pyright_existing_install_invalid")

    visit(root, ())
    return snapshots


def _existing_entry_kind(parent: _Handle) -> str | None:
    try:
        return _entry_kind(parent, _profile.PYRIGHT_VERSION)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, PermissionError) as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc


def _required_snapshot_entries(
    snapshots: dict[str, _ExistingEntry],
) -> tuple[_ExistingEntry, ...]:
    entries = (
        snapshots.get("install-manifest.json"),
        snapshots.get("package"),
        snapshots.get("package/package.json"),
        snapshots.get(_profile.PYRIGHT_SERVER_RELATIVE.as_posix()),
    )
    if any(entry is None or entry.identity is None for entry in entries):
        raise PyrightInstallError("pyright_existing_install_invalid")
    return entries


def _validated_manifest_fields(manifest: dict[str, object]) -> dict[str, object]:
    try:
        return _profile.validate_pyright_install_manifest(manifest)
    except ValueError as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc


def _validated_manifest(
    installation: _Handle, manifest_entry: _ExistingEntry, deadline: float
) -> tuple[bytes, dict[str, object]]:
    manifest_raw = _read_bounded_file(
        installation,
        "install-manifest.json",
        _profile.MAX_INSTALL_MANIFEST_BYTES,
        deadline,
        expected_identity=manifest_entry.identity,
        expected_size=manifest_entry.size,
    )
    manifest = _strict_json_object(manifest_raw, "pyright_existing_install_invalid")
    if canonical_json_bytes(manifest) != manifest_raw:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return manifest_raw, _validated_manifest_fields(manifest)


def _read_package_files(
    package: _Handle,
    package_json_entry: _ExistingEntry,
    server_entry: _ExistingEntry,
    deadline: float,
) -> bytes:
    package_raw = _read_bounded_file(
        package,
        "package.json",
        _profile.MAX_PACKAGE_JSON_BYTES,
        deadline,
        expected_identity=package_json_entry.identity,
        expected_size=package_json_entry.size,
    )
    _validate_package_json(package_raw)
    return _read_bounded_file(
        package,
        "langserver.index.js",
        min(MAX_MEMBER_BYTES, _profile.MAX_SERVER_BYTES),
        deadline,
        expected_identity=server_entry.identity,
        expected_size=server_entry.size,
    )


def _verified_server_digest(
    installation: _Handle,
    entries: tuple[_ExistingEntry, ...],
    validated: dict[str, object],
    deadline: float,
) -> str:
    package = _open_expected_child(
        installation,
        "package",
        directory=True,
        expected_identity=entries[1].identity,
    )
    try:
        server_raw = _read_package_files(package, entries[2], entries[3], deadline)
    finally:
        package.close()
    if not server_raw:
        raise PyrightInstallError("pyright_existing_install_invalid")
    server_sha256 = hashlib.sha256(server_raw).hexdigest()
    if not hmac.compare_digest(server_sha256, validated["server_sha256"]):
        raise PyrightInstallError("pyright_existing_install_invalid")
    return server_sha256


def _install_from_snapshots(
    installation: _Handle, root: Path, deadline: float
) -> InstalledPyright:
    snapshots = _validate_existing_tree(installation, deadline)
    entries = _required_snapshot_entries(snapshots)
    manifest_raw, validated = _validated_manifest(installation, entries[0], deadline)
    server_sha256 = _verified_server_digest(
        installation, entries, validated, deadline
    )
    return InstalledPyright(
        root=root,
        version=_profile.PYRIGHT_VERSION,
        package_sha256=_profile.PYRIGHT_PACKAGE_SHA256,
        package_integrity=_profile.PYRIGHT_PACKAGE_INTEGRITY,
        server_sha256=server_sha256,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
    )


def _require_installation_still_named(
    parent: _Handle, identity: tuple[object, ...]
) -> None:
    if (
        _named_known_identity(parent, _profile.PYRIGHT_VERSION, directory=True)
        != identity
    ):
        raise PyrightInstallError("pyright_existing_install_invalid")


def _validated_existing_install(
    parent: _Handle, root: Path, deadline: float
) -> InstalledPyright:
    installation = _open_known_child(
        parent, _profile.PYRIGHT_VERSION, directory=True
    )
    installation_identity = _identity(installation)
    try:
        result = _install_from_snapshots(installation, root, deadline)
        _require_installation_still_named(parent, installation_identity)
        return result
    finally:
        installation.close()


def _existing_install_or_invalid(
    parent: _Handle, root: Path, deadline: float
) -> InstalledPyright:
    """Every way this can go wrong reads as one code: the install is invalid."""
    try:
        return _validated_existing_install(parent, root, deadline)
    except TimeoutError:
        raise
    except PyrightInstallError as exc:
        if exc.code == "pyright_existing_install_invalid":
            raise
        raise PyrightInstallError("pyright_existing_install_invalid") from exc
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc


def _existing_result(
    parent: _Handle,
    root: Path,
    deadline: float,
) -> InstalledPyright | None:
    _check_deadline(deadline)
    kind = _existing_entry_kind(parent)
    if kind is None:
        return None
    if kind != "directory":
        raise PyrightInstallError("pyright_existing_install_invalid")
    return _existing_install_or_invalid(parent, root, deadline)


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


def _require_plain_artifact(artifact: Path) -> os.stat_result:
    before = artifact.lstat()
    if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode):
        raise PermissionError("artifact is not a regular local file")
    if before.st_size > MAX_COMPRESSED_BYTES:
        raise PyrightInstallError("pyright_archive_compressed_limit")
    return before


def _artifact_posix_identity(before: os.stat_result) -> object | None:
    if os.name != "posix":
        return None
    return _posix_source_identity(before)


def _open_artifact_source(artifact: Path, parent: _Handle) -> _Handle:
    if os.name == "nt":
        return _Handle(
            _windows_workspace.open_exclusive_readonly_source_file(artifact), False
        )
    return _open_child_file(parent, artifact.name)


def _require_unchanged_source(
    source: _Handle, before: os.stat_result, before_posix_identity: object | None
) -> None:
    if _file_size(source) != before.st_size:
        raise PermissionError("artifact changed before open")
    if before_posix_identity is None:
        return
    if _posix_source_identity(os.fstat(source.value)) != before_posix_identity:
        raise PermissionError("artifact changed before open")


def _artifact_is_stable(
    before: os.stat_result,
    after: os.stat_result,
    before_posix_identity: object | None,
) -> bool:
    if before_posix_identity is not None:
        return _posix_source_identity(after) == before_posix_identity
    return (
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


def _require_artifact_unchanged_after(
    artifact: Path,
    parent: _Handle,
    before: os.stat_result,
    before_posix_identity: object | None,
    source_identity: tuple[object, ...],
) -> None:
    after = artifact.lstat()
    if not _artifact_is_stable(before, after, before_posix_identity):
        raise PermissionError("artifact changed or was replaced during copy")
    if _named_identity(parent, artifact.name, directory=False) != source_identity:
        raise PermissionError("artifact changed or was replaced during copy")


def _copy_stable_artifact(
    artifact: Path,
    parent: _Handle,
    destination: _OwnedFile,
    deadline: float,
    before: os.stat_result,
    before_posix_identity: object | None,
) -> tuple[str, bytes]:
    source = _open_artifact_source(artifact, parent)
    source_identity = _identity(source)
    try:
        _require_unchanged_source(source, before, before_posix_identity)
        result = _copy_to_owned_file(source, destination, deadline)
        if _identity(source) != source_identity:
            raise PermissionError("artifact changed during copy")
    finally:
        source.close()
    _require_artifact_unchanged_after(
        artifact, parent, before, before_posix_identity, source_identity
    )
    return result


def _copy_verified_artifact(
    artifact: Path, destination: _OwnedFile, deadline: float
) -> tuple[str, bytes]:
    _validate_existing_directory_chain(artifact.parent, deadline)
    _require_local_filesystem(artifact, deadline)
    before = _require_plain_artifact(artifact)
    before_posix_identity = _artifact_posix_identity(before)
    parent = _open_absolute_directory(artifact.parent)
    try:
        return _copy_stable_artifact(
            artifact, parent, destination, deadline, before, before_posix_identity
        )
    finally:
        parent.close()


def _copy_local_artifact(
    artifact: Path,
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    _check_deadline(deadline)
    _require_absolute_local(artifact, "artifact")
    try:
        return _copy_verified_artifact(artifact, destination, deadline)
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


def _response_timeout_setter(response: object) -> object:
    try:
        set_timeout = response.fp.raw._sock.settimeout
    except (AttributeError, TypeError) as exc:
        raise PyrightInstallError("pyright_download_response_invalid") from exc
    if not callable(set_timeout):
        raise PyrightInstallError("pyright_download_response_invalid")
    return set_timeout


def _set_response_read_timeout(response: object, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Pyright installation deadline expired")
    set_timeout = _response_timeout_setter(response)
    try:
        set_timeout(min(NETWORK_TIMEOUT_SECONDS, remaining))
    except TimeoutError:
        raise
    except (TypeError, ValueError) as exc:
        raise PyrightInstallError("pyright_download_response_invalid") from exc


def _http_error_code(status: int) -> str:
    if 300 <= status < 400:
        return "pyright_download_redirect"
    return "pyright_download_failed"


def _download_http_error(exc: urllib.error.HTTPError) -> PyrightInstallError:
    code = _http_error_code(exc.code)
    try:
        exc.close()
    except Exception as close_error:
        exc.__cause__ = close_error
    return PyrightInstallError(code)


def _opened_download(deadline: float) -> object:
    timeout = min(NETWORK_TIMEOUT_SECONDS, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Pyright installation deadline expired")
    request = urllib.request.Request(
        _profile.PYRIGHT_PACKAGE_URL,
        method="GET",
        headers={"Accept": "application/octet-stream"},
    )
    try:
        return _open_pinned_url(request, timeout=timeout)
    except TimeoutError:
        raise
    except urllib.error.HTTPError as exc:
        raise _download_http_error(exc) from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise PyrightInstallError("pyright_download_failed") from exc


def _require_pinned_response(response: object) -> None:
    if getattr(response, "status", None) != 200:
        raise PyrightInstallError("pyright_download_status")
    if response.geturl() != _profile.PYRIGHT_PACKAGE_URL:
        raise PyrightInstallError("pyright_download_url_drift")


def _download_content_length(response: object) -> int | None:
    content_length = _response_content_length(response)
    if content_length is not None and content_length > MAX_COMPRESSED_BYTES:
        raise PyrightInstallError("pyright_archive_compressed_limit")
    return content_length


def _response_reader(response: object) -> object:
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        raise PyrightInstallError("pyright_download_response_invalid")
    return read1


def _next_download_chunk(response: object, read1: object, deadline: float) -> bytes:
    try:
        _set_response_read_timeout(response, deadline)
        chunk = read1(COPY_CHUNK_BYTES)
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_download_failed") from exc
    if not isinstance(chunk, bytes):
        raise PyrightInstallError("pyright_download_response_invalid")
    return chunk


def _stream_download(
    response: object,
    destination: _OwnedFile,
    deadline: float,
    content_length: int | None,
    sha256: object,
    sha512: object,
) -> int:
    read1 = _response_reader(response)
    total = 0
    while content_length is None or total < content_length:
        _check_deadline(deadline)
        chunk = _next_download_chunk(response, read1, deadline)
        _check_deadline(deadline)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_COMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_compressed_limit")
        _write_handle(destination.handle, chunk, deadline)
        sha256.update(chunk)
        sha512.update(chunk)
    return total


def _downloaded_bytes(
    response_context: object, destination: _OwnedFile, deadline: float
) -> tuple[str, bytes]:
    with response_context as response:
        _require_pinned_response(response)
        content_length = _download_content_length(response)
        sha256, sha512 = _new_hashes()
        total = _stream_download(
            response, destination, deadline, content_length, sha256, sha512
        )
        if content_length is not None and total != content_length:
            raise PyrightInstallError("pyright_download_truncated")
    _check_deadline(deadline)
    _checked_fsync_file(destination.handle)
    return sha256.hexdigest(), sha512.digest()


def _download_artifact(
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    _check_deadline(deadline)
    response_context = _opened_download(deadline)
    try:
        return _downloaded_bytes(response_context, destination, deadline)
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


def _pax_field_size(key: str, value: str) -> int:
    try:
        return len(key.encode("utf-8")) + len(value.encode("utf-8")) + 2
    except (AttributeError, UnicodeError) as exc:
        raise PyrightInstallError("pyright_archive_pax_limit") from exc


def _require_supported_pax_key(key: str) -> None:
    if key.startswith("GNU.sparse") or key in {"SCHILY.realsize", "size"}:
        raise PyrightInstallError("pyright_archive_member_type")


def _pax_size(member: tarfile.TarInfo) -> int:
    headers = member.pax_headers
    if len(headers) > MAX_PAX_FIELDS:
        raise PyrightInstallError("pyright_archive_pax_limit")
    total = 0
    for key, value in headers.items():
        total += _pax_field_size(key, value)
        if total > MAX_PAX_BYTES:
            raise PyrightInstallError("pyright_archive_pax_limit")
        _require_supported_pax_key(key)
    return total


def _member_name(member: tarfile.TarInfo) -> str:
    name = member.name
    if not isinstance(name, str):
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if member.isdir() and name.endswith("/"):
        return name[:-1]
    return name


def _require_bounded_member_name(name: str) -> None:
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PyrightInstallError("pyright_archive_path_unsafe") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise PyrightInstallError("pyright_archive_path_limit")


def _archive_name_has_no_windows_root(name: str) -> bool:
    windows = PureWindowsPath(name)
    if windows.drive or windows.root:
        return False
    return _is_plain_unicode_name(name)


def _archive_name_is_safe(name: str) -> bool:
    if not name or name.startswith("/"):
        return False
    if "\\" in name:
        return False
    return _archive_name_has_no_windows_root(name)


def _require_safe_archive_name(name: str) -> None:
    if not _archive_name_is_safe(name):
        raise PyrightInstallError("pyright_archive_path_unsafe")


def _require_package_root(parts: tuple[str, ...]) -> None:
    if len(parts) > MAX_PATH_COMPONENTS:
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if any(part in {"", ".", ".."} for part in parts):
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if parts[0] != "package":
        raise PyrightInstallError("pyright_archive_path_unsafe")


def _archive_component_size(part: str) -> int:
    try:
        return len(part.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise PyrightInstallError("pyright_archive_path_unsafe") from exc


def _require_safe_archive_component(part: str) -> None:
    if _archive_component_size(part) > MAX_PATH_COMPONENT_BYTES:
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if part != part.rstrip(" .") or ":" in part:
        raise PyrightInstallError("pyright_archive_path_unsafe")
    if part.rstrip(" .").split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise PyrightInstallError("pyright_archive_path_unsafe")


def _member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    name = _member_name(member)
    _require_bounded_member_name(name)
    _require_safe_archive_name(name)
    parts = tuple(name.split("/"))
    _require_package_root(parts)
    for part in parts:
        _require_safe_archive_component(part)
    return parts


class _ArchiveNames:
    def __init__(self) -> None:
        self.kinds: dict[str, str] = {}
        self.explicit: set[str] = set()
        self.folded: dict[str, str] = {}

    def add(self, parts: tuple[str, ...], kind: str) -> None:
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            final = index == len(parts)
            expected_kind = kind if final else "directory"
            self._add_one(path, expected_kind, final=final)
        self.explicit.add("/".join(parts))

    def _add_one(self, path: str, expected_kind: str, *, final: bool) -> None:
        self._require_no_fold_collision(path)
        self._require_same_kind(path, expected_kind)
        if final and path in self.explicit:
            raise PyrightInstallError("pyright_archive_duplicate_member")
        if expected_kind == "file":
            self._require_no_directory_at(path)
        self.kinds[path] = expected_kind

    def _require_no_fold_collision(self, path: str) -> None:
        """Two names that fold together collide on a case-folding filesystem."""
        folded = unicodedata.normalize("NFC", path).casefold()
        previous_folded = self.folded.get(folded)
        if previous_folded is not None and previous_folded != path:
            raise PyrightInstallError("pyright_archive_name_collision")
        self.folded[folded] = path

    def _require_same_kind(self, path: str, expected_kind: str) -> None:
        previous_kind = self.kinds.get(path)
        if previous_kind is not None and previous_kind != expected_kind:
            raise PyrightInstallError("pyright_archive_path_conflict")

    def _require_no_directory_at(self, path: str) -> None:
        if any(existing.startswith(path + "/") for existing in self.kinds):
            raise PyrightInstallError("pyright_archive_path_conflict")


def _member_chunk(source: io.BufferedReader, remaining: int) -> bytes:
    chunk = source.read(min(COPY_CHUNK_BYTES, remaining))
    if not isinstance(chunk, bytes) or not chunk:
        raise PyrightInstallError("pyright_archive_truncated_member")
    if len(chunk) > remaining:
        raise PyrightInstallError("pyright_archive_malformed")
    return chunk


def _stream_member(
    source: io.BufferedReader,
    destination: _Handle,
    size: int,
    deadline: float,
    digest: object,
    captured: bytearray | None,
) -> None:
    remaining = size
    while remaining:
        _check_deadline(deadline)
        chunk = _member_chunk(source, remaining)
        _write_handle(destination, chunk, deadline)
        digest.update(chunk)
        if captured is not None:
            captured.extend(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise PyrightInstallError("pyright_archive_malformed")


def _captured_bytes(captured: bytearray | None) -> bytes | None:
    if captured is None:
        return None
    return bytes(captured)


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
    try:
        _stream_member(source, destination, size, deadline, digest, captured)
        _check_deadline(deadline)
        _checked_fsync_file(destination)
    finally:
        destination.close()
    return digest.hexdigest(), _captured_bytes(captured)


def _require_artifact_state(
    artifact: _OwnedFile, accepted_state: tuple[object, ...]
) -> None:
    if _artifact_state(artifact.handle) != accepted_state:
        raise PyrightInstallError("pyright_artifact_changed")


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    return "file"


def _require_supported_member_type(member: tarfile.TarInfo) -> None:
    if member.sparse is not None:
        raise PyrightInstallError("pyright_archive_member_type")
    if not (member.isdir() or member.isreg()):
        raise PyrightInstallError("pyright_archive_member_type")


def _require_empty_directory_member(member: tarfile.TarInfo) -> None:
    if member.size != 0:
        raise PyrightInstallError("pyright_archive_member_type")


def _account_member(member: tarfile.TarInfo, state: dict[str, int]) -> None:
    """Charge this member's hidden metadata and count against the bounds."""
    hidden_metadata = (
        member.offset
        - state["offset"]
        + member.offset_data
        - member.offset
        - tarfile.BLOCKSIZE
    )
    if hidden_metadata < 0 or hidden_metadata > MAX_PAX_BYTES:
        raise PyrightInstallError("pyright_archive_pax_limit")
    state["metadata"] += hidden_metadata
    if state["metadata"] > MAX_EXTENDED_METADATA_BYTES:
        raise PyrightInstallError("pyright_archive_pax_limit")
    state["offset"] = member.offset_data + (
        (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE * tarfile.BLOCKSIZE
    )
    state["members"] += 1
    if state["members"] > MAX_MEMBERS:
        raise PyrightInstallError("pyright_archive_member_count_limit")


def _account_member_size(member: tarfile.TarInfo, state: dict[str, int]) -> None:
    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
        raise PyrightInstallError("pyright_archive_member_limit")
    state["aggregate"] += member.size
    if state["aggregate"] > MAX_TOTAL_FILE_BYTES:
        raise PyrightInstallError("pyright_archive_aggregate_limit")


def _member_findings(
    package_member: bool, server_member: bool, captured: bytes | None, digest: str
) -> tuple[bytes | None, str | None]:
    package_json = captured if package_member else None
    server_sha256 = digest if server_member else None
    return package_json, server_sha256


def _extract_file_member(
    stage: _Stage,
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    parts: tuple[str, ...],
    deadline: float,
) -> tuple[bytes | None, str | None]:
    package_member = parts == ("package", "package.json")
    server_member = parts == tuple(_profile.PYRIGHT_SERVER_RELATIVE.parts)
    if package_member and member.size > _profile.MAX_PACKAGE_JSON_BYTES:
        raise PyrightInstallError("pyright_package_json_oversized")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise PyrightInstallError("pyright_archive_malformed")
    with extracted:
        digest, captured = _copy_member_data(
            stage, parts, extracted, member.size, deadline, capture=package_member
        )
    return _member_findings(package_member, server_member, captured, digest)


def _extract_one_member(
    stage: _Stage,
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    names: _ArchiveNames,
    state: dict[str, int],
    deadline: float,
) -> tuple[bytes | None, str | None]:
    _pax_size(member)
    _require_supported_member_type(member)
    parts = _member_parts(member)
    names.add(parts, _member_kind(member))
    if member.isdir():
        _require_empty_directory_member(member)
        stage.ensure_directory(parts, deadline)
        return None, None
    _account_member_size(member, state)
    return _extract_file_member(stage, archive, member, parts, deadline)


def _merge_found(
    package_json: bytes | None,
    server_sha256: str | None,
    found: tuple[bytes | None, str | None],
) -> tuple[bytes | None, str | None]:
    if found[0] is not None:
        package_json = found[0]
    if found[1] is not None:
        server_sha256 = found[1]
    return package_json, server_sha256


def _require_no_trailing_bytes(bounded: io.BufferedReader, deadline: float) -> None:
    while True:
        _check_deadline(deadline)
        trailing = bounded.read(COPY_CHUNK_BYTES)
        if not isinstance(trailing, bytes):
            raise PyrightInstallError("pyright_archive_malformed")
        if not trailing:
            return


def _read_archive_members(
    stage: _Stage, bounded: io.BufferedReader, deadline: float
) -> tuple[bytes | None, str | None, _ArchiveNames]:
    names = _ArchiveNames()
    state = {"members": 0, "aggregate": 0, "offset": 0, "metadata": 0}
    package_json: bytes | None = None
    server_sha256: str | None = None
    with tarfile.open(fileobj=bounded, mode="r|") as archive:
        for member in archive:
            _check_deadline(deadline)
            _account_member(member, state)
            found = _extract_one_member(
                stage, archive, member, names, state, deadline
            )
            package_json, server_sha256 = _merge_found(
                package_json, server_sha256, found
            )
    _require_no_trailing_bytes(bounded, deadline)
    return package_json, server_sha256, names


def _extract_members(
    stage: _Stage, artifact: _OwnedFile, deadline: float
) -> tuple[bytes | None, str | None, _ArchiveNames]:
    _seek_start(artifact.handle)
    raw = io.BufferedReader(
        _HandleReader(artifact.handle), buffer_size=COPY_CHUNK_BYTES
    )
    compressed = gzip.GzipFile(fileobj=raw, mode="rb")
    bounded_raw = _BoundedDecompressedReader(compressed, deadline)
    bounded = io.BufferedReader(bounded_raw, buffer_size=COPY_CHUNK_BYTES)
    try:
        return _read_archive_members(stage, bounded, deadline)
    except gzip.BadGzipFile as exc:
        raise PyrightInstallError("pyright_archive_malformed") from exc
    except (tarfile.TarError, EOFError) as exc:
        raise PyrightInstallError("pyright_archive_malformed") from exc
    finally:
        bounded.close()
        compressed.close()
        raw.close()


def _require_extracted_server(
    package_json: bytes | None, server_sha256: str | None, names: _ArchiveNames
) -> None:
    if package_json is None:
        raise PyrightInstallError("pyright_package_json_missing")
    if server_sha256 is None:
        raise PyrightInstallError("pyright_server_missing")
    if names.kinds.get(_profile.PYRIGHT_SERVER_RELATIVE.as_posix()) != "file":
        raise PyrightInstallError("pyright_server_missing")
    if server_sha256 == hashlib.sha256(b"").hexdigest():
        raise PyrightInstallError("pyright_server_empty")


def _digests_match(
    final_sha256: str,
    final_sha512: bytes,
    accepted_sha256: str,
    accepted_sha512: bytes,
) -> bool:
    if not hmac.compare_digest(final_sha256, accepted_sha256):
        return False
    return hmac.compare_digest(final_sha512, accepted_sha512)


def _require_artifact_digests(
    artifact: _OwnedFile,
    deadline: float,
    accepted_state: tuple[object, ...],
    accepted_sha256: str,
    accepted_sha512: bytes,
) -> None:
    """The bytes that were extracted must still be the bytes that were accepted."""
    _seek_start(artifact.handle)
    final_sha256, final_sha512 = _hash_handle(artifact.handle, deadline)
    if not _digests_match(
        final_sha256, final_sha512, accepted_sha256, accepted_sha512
    ):
        raise PyrightInstallError("pyright_artifact_changed")
    _require_artifact_state(artifact, accepted_state)


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
        _require_artifact_state(artifact, accepted_state)
        package_json, server_sha256, names = _extract_members(
            stage, artifact, deadline
        )
        _require_extracted_server(package_json, server_sha256, names)
        _require_artifact_digests(
            artifact, deadline, accepted_state, accepted_sha256, accepted_sha512
        )
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


def _bound_rename(library: object, symbol: str) -> object:
    function = getattr(library, symbol, None)
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
    return function


def _noreplace_rename() -> tuple[object, int]:
    """The platform's rename that refuses to replace, with its flag."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        return _bound_rename(library, "renameat2"), 1
    if sys.platform == "darwin":
        return _bound_rename(library, "renameatx_np"), 0x00000004
    raise PyrightInstallError("pyright_publish_unsupported")


def _posix_publish_noreplace(stage: _Stage, parent: _Handle, name: str) -> int:
    old_name = os.fsencode(stage.name)
    new_name = os.fsencode(name)
    function, flag = _noreplace_rename()
    return function(parent.value, old_name, parent.value, new_name, flag)


def _publish_error(error: int) -> OSError:
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        return FileExistsError(error, "Pyright installation already exists")
    return OSError(error, "could not publish Pyright installation")


def _atomic_publish_noreplace(stage: _Stage, parent: _Handle, name: str) -> None:
    if os.name == "nt":
        _windows_workspace.publish_file(stage.root.value, parent.value, name)
        stage.published = True
        return
    if _posix_publish_noreplace(stage, parent, name) == 0:
        stage.published = True
        return
    raise _publish_error(ctypes.get_errno())


def _fetched_artifact(
    artifact: Path | None, temporary: _OwnedFile, deadline: float
) -> tuple[str, bytes]:
    if artifact is None:
        return _download_artifact(temporary, deadline)
    return _copy_local_artifact(artifact, temporary, deadline)


def _staged_manifest(stage: _Stage, server_sha256: str, deadline: float) -> bytes:
    manifest = _profile.build_pyright_install_manifest(server_sha256=server_sha256)
    manifest_bytes = canonical_json_bytes(manifest)
    stage.write_bytes(("install-manifest.json",), manifest_bytes, deadline)
    stage.sync_directories(deadline)
    return manifest_bytes


def _published_by_another(
    parent: _Handle, root: Path, deadline: float
) -> InstalledPyright:
    existing = _existing_result(parent, root, deadline)
    if existing is None:
        raise PyrightInstallError("pyright_publish_race")
    return existing


def _publish_stage(
    stage: _Stage, parent: _Handle, root: Path, deadline: float
) -> InstalledPyright | None:
    """The install someone else published first, or None when this one won."""
    try:
        _atomic_publish_noreplace(stage, parent, _profile.PYRIGHT_VERSION)
    except FileExistsError:
        return _published_by_another(parent, root, deadline)
    except (PyrightInstallError, TimeoutError):
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_publish_failed") from exc
    stage.published = True
    _check_deadline(deadline)
    _checked_fsync_directory(parent)
    return None


def _installed_result(
    root: Path, server_sha256: str, manifest_bytes: bytes
) -> InstalledPyright:
    return InstalledPyright(
        root=root,
        version=_profile.PYRIGHT_VERSION,
        package_sha256=_profile.PYRIGHT_PACKAGE_SHA256,
        package_integrity=_profile.PYRIGHT_PACKAGE_INTEGRITY,
        server_sha256=server_sha256,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


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
        package_sha256, package_sha512 = _fetched_artifact(
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
        manifest_bytes = _staged_manifest(stage, server_sha256, deadline)
        _check_deadline(deadline)
        published = _publish_stage(stage, parent, root, deadline)
        if published is not None:
            return published
        return _installed_result(root, server_sha256, manifest_bytes)
    finally:
        if stage is not None:
            stage.cleanup()
        temporary.cleanup()


def _require_install_arguments(state_root: object, artifact: object) -> None:
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    if artifact is not None and not isinstance(artifact, Path):
        raise TypeError("artifact must be a Path or None")


def _opened_runtime_parent(state_root: Path, deadline: float) -> _Handle:
    try:
        return _ensure_runtime_parent(state_root, deadline)
    except TimeoutError:
        raise
    except PyrightInstallError:
        raise
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise PyrightInstallError("pyright_state_root_unsafe") from exc


def _awaited_lock(parent: _Handle, deadline: float) -> _OwnedLock:
    """Wait for the install lock, polling until the caller's deadline expires."""
    while True:
        _check_deadline(deadline)
        lock = _try_create_lock(parent, deadline)
        if lock is not None:
            return lock
        time.sleep(min(LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _install_holding_lock(
    parent: _Handle, root: Path, artifact: Path | None, deadline: float
) -> InstalledPyright:
    """Someone may have published while this call waited for the lock."""
    existing = _existing_result(parent, root, deadline)
    if existing is not None:
        return existing
    return _install_under_lock(parent, root, artifact, deadline)


def _release_lock(lock: _OwnedLock | None) -> None:
    if lock is not None:
        lock.cleanup()


def _install_after_waiting(
    parent: _Handle, root: Path, artifact: Path | None, deadline: float
) -> InstalledPyright:
    lock: _OwnedLock | None = None
    try:
        lock = _awaited_lock(parent, deadline)
        return _install_holding_lock(parent, root, artifact, deadline)
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_install_io_failed") from exc
    finally:
        _release_lock(lock)


def _install_with_parent(
    parent: _Handle, root: Path, artifact: Path | None, deadline: float
) -> InstalledPyright:
    try:
        existing = _existing_result(parent, root, deadline)
    except TimeoutError:
        raise
    except OSError as exc:
        raise PyrightInstallError("pyright_install_io_failed") from exc
    if existing is not None:
        return existing
    return _install_after_waiting(parent, root, artifact, deadline)


def install_pyright(
    *,
    state_root: Path,
    artifact: Path | None = None,
    deadline: float | None = None,
) -> InstalledPyright:
    """Install the pinned release after explicit invocation, or validate it."""
    _require_install_arguments(state_root, artifact)
    effective_deadline = _validated_deadline(deadline)
    _check_deadline(effective_deadline)
    _prepare_state_root(state_root, effective_deadline)
    root = managed_pyright_root(state_root)
    parent = _opened_runtime_parent(state_root, effective_deadline)
    try:
        return _install_with_parent(parent, root, artifact, effective_deadline)
    finally:
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
