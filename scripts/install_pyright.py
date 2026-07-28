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
from reliable_memory import (
    UnsafeStateRoot,
    _known_network_path,
    canonical_json_bytes,
    validate_state_root,
)

DEFAULT_INSTALL_TIMEOUT_SECONDS = 120.0
NETWORK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.01
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
        try:
            _clear_directory(self.root)
            if os.name == "nt":
                _windows_workspace.delete_handle(self.root.value)
            elif _named_identity(self.parent, self.name, directory=True) == self.root_identity:
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


def _validate_existing_directory_chain(path: Path) -> None:
    candidates = [path, *path.parents]
    for candidate in reversed(candidates):
        if candidate == Path(candidate.anchor):
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError("runtime path contains a link, reparse point, or non-directory")


def _prepare_state_root(state_root: Path, deadline: float) -> None:
    _check_deadline(deadline)
    _require_absolute_local(state_root, "state_root")
    try:
        _validate_existing_directory_chain(state_root)
        if _known_network_path(state_root):
            raise PermissionError("state root is on a network filesystem")
        _check_deadline(deadline)
        validate_state_root(state_root)
        _check_deadline(deadline)
        _validate_existing_directory_chain(state_root / "cache/code-tools/pyright")
        if _known_network_path(state_root / "cache/code-tools/pyright"):
            raise PermissionError("runtime hierarchy is on a network filesystem")
    except (OSError, RuntimeError, UnsafeStateRoot, ValueError) as exc:
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


def _open_absolute_directory(path: Path) -> _Handle:
    if os.name == "nt":
        return _Handle(_windows_workspace.open_directory_path(path), True)
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
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    return _Handle(os.open(name, flags, 0o600, dir_fd=parent.value), False)


def _open_child_file(parent: _Handle, name: str, *, shared: bool = False) -> _Handle:
    if _entry_kind(parent, name) != "file":
        raise PermissionError("expected a regular file")
    if os.name == "nt":
        function = (
            _windows_workspace.open_shared_readonly_source_file
            if shared
            else _windows_workspace.open_file
        )
        return _Handle(function(parent.value, name), False)
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
    except OSError as exc:
        raise PyrightInstallError("pyright_install_fsync_failed") from exc


def _checked_fsync_directory(handle: _Handle) -> None:
    try:
        _fsync_directory(handle)
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
    current = _open_absolute_directory(state_root)
    try:
        for component in ("cache", "code-tools", "pyright"):
            _check_deadline(deadline)
            kind = _entry_kind(current, component)
            if kind is None:
                try:
                    child = _create_child_directory(current, component)
                except FileExistsError:
                    if _entry_kind(current, component) != "directory":
                        raise PermissionError(
                            "runtime hierarchy creation lost to an unsafe entry"
                        )
                    child = _open_child_directory(current, component, writable=True)
                else:
                    _check_deadline(deadline)
                    if os.name == "nt":
                        _windows_workspace.flush_directory(current.value)
                    else:
                        _fsync_directory(current)
            elif kind == "directory":
                child = _open_child_directory(current, component, writable=True)
            else:
                raise PermissionError("runtime hierarchy contains an unsafe entry")
            current.close()
            current = child
        return current
    except BaseException:
        current.close()
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


def _try_create_lock(parent: _Handle, deadline: float) -> _OwnedFile | None:
    try:
        handle = _create_child_file(parent, _LOCK_NAME)
    except FileExistsError:
        kind = _entry_kind(parent, _LOCK_NAME)
        if kind != "file":
            raise PyrightInstallError("pyright_install_lock_unsafe")
        return None
    owned = _OwnedFile(parent, _LOCK_NAME, handle, _identity(handle))
    try:
        _write_handle(handle, secrets.token_hex(16).encode("ascii"), deadline)
        _check_deadline(deadline)
        _checked_fsync_file(handle)
        return owned
    except BaseException:
        owned.cleanup()
        raise


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
) -> bytes:
    _check_deadline(deadline)
    handle = _open_child_file(parent, name)
    identity = _identity(handle)
    try:
        if _file_size(handle) > maximum:
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
        if _identity(handle) != identity:
            raise PyrightInstallError("pyright_existing_install_invalid")
    finally:
        handle.close()
    if _named_identity(parent, name, directory=False) != identity:
        raise PyrightInstallError("pyright_existing_install_invalid")
    return bytes(content)


def _existing_directory_entries(directory: _Handle) -> tuple[tuple[str, str], ...]:
    if os.name == "nt":
        return tuple(
            (entry.name, entry.kind)
            for entry in _windows_workspace.list_directory(
                directory.value, max_entries=MAX_MEMBERS + 2
            )
        )
    values: list[tuple[str, str]] = []
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
            values.append((entry.name, kind))
    return tuple(sorted(values))


def _validate_existing_tree(root: _Handle, deadline: float) -> None:
    count = 0
    total_bytes = 0
    folded_paths: dict[str, str] = {}

    def visit(directory: _Handle, parts: tuple[str, ...]) -> None:
        nonlocal count, total_bytes
        _check_deadline(deadline)
        entries = _existing_directory_entries(directory)
        if not parts and {name for name, _kind in entries} != {
            "install-manifest.json",
            "package",
        }:
            raise PyrightInstallError("pyright_existing_install_invalid")
        for name, kind in entries:
            _check_deadline(deadline)
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
                child = _open_child_directory(directory, name)
                identity = _identity(child)
                try:
                    visit(child, child_parts)
                finally:
                    child.close()
                if _named_identity(directory, name, directory=True) != identity:
                    raise PyrightInstallError("pyright_existing_install_invalid")
            elif kind == "file":
                child = _open_child_file(directory, name)
                identity = _identity(child)
                try:
                    size = _file_size(child)
                    if size > MAX_MEMBER_BYTES:
                        raise PyrightInstallError("pyright_existing_install_invalid")
                    total_bytes += size
                    if total_bytes > MAX_TOTAL_FILE_BYTES + _profile.MAX_INSTALL_MANIFEST_BYTES:
                        raise PyrightInstallError("pyright_existing_install_invalid")
                finally:
                    child.close()
                if _named_identity(directory, name, directory=False) != identity:
                    raise PyrightInstallError("pyright_existing_install_invalid")
            else:
                raise PyrightInstallError("pyright_existing_install_invalid")

    visit(root, ())


def _existing_result(
    parent: _Handle,
    root: Path,
    deadline: float,
) -> InstalledPyright | None:
    _check_deadline(deadline)
    try:
        kind = _entry_kind(parent, _profile.PYRIGHT_VERSION)
    except (OSError, RuntimeError, PermissionError) as exc:
        raise PyrightInstallError("pyright_existing_install_invalid") from exc
    if kind is None:
        return None
    if kind != "directory":
        raise PyrightInstallError("pyright_existing_install_invalid")
    try:
        installation = _open_child_directory(parent, _profile.PYRIGHT_VERSION)
        try:
            _validate_existing_tree(installation, deadline)
            manifest_raw = _read_bounded_file(
                installation,
                "install-manifest.json",
                _profile.MAX_INSTALL_MANIFEST_BYTES,
                deadline,
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
            package = _open_child_directory(installation, "package")
            try:
                package_raw = _read_bounded_file(
                    package,
                    "package.json",
                    _profile.MAX_PACKAGE_JSON_BYTES,
                    deadline,
                )
                _validate_package_json(package_raw)
                server_raw = _read_bounded_file(
                    package,
                    "langserver.index.js",
                    min(MAX_MEMBER_BYTES, _profile.MAX_SERVER_BYTES),
                    deadline,
                )
            finally:
                package.close()
        finally:
            installation.close()
        if not server_raw:
            raise PyrightInstallError("pyright_existing_install_invalid")
        server_sha256 = hashlib.sha256(server_raw).hexdigest()
        if not hmac.compare_digest(server_sha256, validated["server_sha256"]):
            raise PyrightInstallError("pyright_existing_install_invalid")
        return InstalledPyright(
            root=root,
            version=_profile.PYRIGHT_VERSION,
            package_sha256=_profile.PYRIGHT_PACKAGE_SHA256,
            package_integrity=_profile.PYRIGHT_PACKAGE_INTEGRITY,
            server_sha256=server_sha256,
            manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        )
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


def _copy_local_artifact(
    artifact: Path,
    destination: _OwnedFile,
    deadline: float,
) -> tuple[str, bytes]:
    _check_deadline(deadline)
    _require_absolute_local(artifact, "artifact")
    try:
        _validate_existing_directory_chain(artifact.parent)
        if _known_network_path(artifact):
            raise PermissionError("artifact is on a network filesystem")
        before = artifact.lstat()
        if _is_reparse_or_link(before) or not stat.S_ISREG(before.st_mode):
            raise PermissionError("artifact is not a regular local file")
        if before.st_size > MAX_COMPRESSED_BYTES:
            raise PyrightInstallError("pyright_archive_compressed_limit")
        parent = _open_absolute_directory(artifact.parent)
        try:
            source = _open_child_file(parent, artifact.name, shared=True)
            source_identity = _identity(source)
            try:
                if _file_size(source) != before.st_size:
                    raise PermissionError("artifact changed before open")
                result = _copy_to_owned_file(source, destination, deadline)
                if _identity(source) != source_identity:
                    raise PermissionError("artifact changed during copy")
            finally:
                source.close()
            after = artifact.lstat()
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
    except urllib.error.HTTPError as exc:
        code = (
            "pyright_download_redirect"
            if 300 <= exc.code < 400
            else "pyright_download_failed"
        )
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
            sha256, sha512 = _new_hashes()
            total = 0
            while True:
                _check_deadline(deadline)
                try:
                    chunk = response.read(COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise PyrightInstallError("pyright_download_failed") from exc
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
) -> tuple[_Stage, bytes, str]:
    _check_deadline(deadline)
    stage = _new_stage(parent)
    source_handle: _Handle | None = None
    try:
        artifact.close()
        source_handle = _open_child_file(parent, artifact.name)
        if _identity(source_handle) != artifact.identity:
            raise PyrightInstallError("pyright_artifact_changed")
        raw = io.BufferedReader(_HandleReader(source_handle), buffer_size=COPY_CHUNK_BYTES)
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
        return stage, package_json, server_sha256
    except OSError as exc:
        stage.cleanup()
        raise PyrightInstallError("pyright_archive_extract_failed") from exc
    except BaseException:
        stage.cleanup()
        raise
    finally:
        if source_handle is not None:
            source_handle.close()


def _atomic_publish_noreplace(stage: _Stage, parent: _Handle, name: str) -> None:
    if os.name == "nt":
        _windows_workspace.publish_file(stage.root.value, parent.value, name)
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
        _verify_artifact_digests(package_sha256, package_sha512)
        _check_deadline(deadline)
        stage, package_json, server_sha256 = _extract_archive(
            parent, temporary, deadline
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
    lock: _OwnedFile | None = None
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options: dict[str, object] = {"state_root": args.state_root}
    if args.artifact is not None:
        options["artifact"] = args.artifact
    try:
        result = install_pyright(**options)
    except (OSError, PyrightInstallError, TimeoutError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
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
