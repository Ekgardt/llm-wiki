"""Contain LSP navigation evidence to a trusted repository and redact logs.

The language server still runs with the operator's permissions. This module is
not a sandbox: it validates requested and returned navigation evidence, while
the repository and configured Pyright process remain trusted.
"""

from __future__ import annotations

import math
import ntpath
import os
import posixpath
import re
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import unquote_to_bytes, urlsplit

import windows_workspace
from lsp_positions import file_uri_to_path, path_to_file_uri
from repository_scope import RepositoryScope

_MAX_RELATIVE_PATH = 4096
_MAX_COMPONENTS = 256
_MAX_COMPONENT_CHARACTERS = 255
_MAX_COMPONENT_BYTES = 255
_MAX_PROVIDER_URI = 16 * 1024
_MAX_DIRECTORY_ENTRIES = 100_000
_MAX_REDACTION_RAW_BYTES = 256 * 1024
_MAX_REDACTION_PATH_TOKEN = 128 * 1024
_SOURCE_READ_CHUNK_BYTES = 64 * 1024
_OVERSIZED_REDACTION_MARKER = "<redacted: oversized LSP log>"
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")
_URL_AUTHORITY_TOKEN_BOUNDARIES = frozenset('"<>\\^`{|}')
_WINDOWS_TOKEN_STRUCTURAL_TERMINATORS = frozenset('<>|?*"')
_WINDOWS_LOG_TRAILING_PUNCTUATION = frozenset(".,;)]}")
_WINDOWS_RESERVED = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"com{number}" for number in "¹²³"),
        *(f"lpt{number}" for number in "¹²³"),
    }
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ai)(?<![a-z0-9_])(?P<quote>[\"']?)[a-z0-9_]*"
    r"(?:api_key|authorization|password|secret|token)"
    r"(?P=quote)\s*[:=]\s*"
)
class PathContainmentError(ValueError):
    """A source path cannot be proven to remain in its repository checkout."""


@dataclass(frozen=True, slots=True)
class RepositorySource:
    repository_id: str
    checkout_id: str
    relative_path: str
    absolute_path: Path
    uri: str


@dataclass(frozen=True, slots=True)
class _TraversalStep:
    name: str
    identity: tuple[object, ...]
    directory: bool


class _OwnedHandles:
    def __init__(self, close: Callable[[int], None]) -> None:
        self._close = close
        self._values: list[int] = []

    def __enter__(self) -> _OwnedHandles:
        return self

    def own(self, value: int) -> int:
        self._values.append(value)
        return value

    def _close_quietly(self, value: int) -> BaseException | None:
        try:
            self._close(value)
        except BaseException as exc:  # close every owned descriptor or handle
            return exc
        return None

    def _close_all(self) -> BaseException | None:
        """Close in reverse order; the first error is the one reported."""
        close_error: BaseException | None = None
        for value in reversed(self._values):
            error = self._close_quietly(value)
            if close_error is None:
                close_error = error
        return close_error

    def __exit__(self, exception_type, _exception, _traceback) -> bool:
        close_error = self._close_all()
        if close_error is not None and exception_type is None:
            raise close_error
        return False


def _resolution_barrier() -> None:
    return


def _is_control(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint < 32
        or 127 <= codepoint <= 159
        or unicodedata.category(character) in {"Cf", "Zl", "Zp"}
    )


# (upper bound, UTF-8 bytes) in ascending order; lone surrogates have none.
_UTF8_WIDTHS = ((0x80, 1), (0x800, 2), (0xD800, 3), (0xE000, 0), (0x10000, 3))


def _utf8_width(codepoint: int) -> int:
    """UTF-8 bytes of one code point; 0 for a lone surrogate, which has none."""
    return next((width for bound, width in _UTF8_WIDTHS if codepoint < bound), 4)


def _fits_utf8_redaction_ceiling(value: str) -> bool:
    byte_count = 0
    for character in value:
        width = _utf8_width(ord(character))
        if width == 0:
            return False
        byte_count += width
        if byte_count > _MAX_REDACTION_RAW_BYTES:
            return False
    return True


def _unnormalized_path_text(value: str) -> bool:
    if unicodedata.normalize("NFC", value) != value:
        return True
    return any(_is_control(character) for character in value)


def _uncanonical_path_text(value: str) -> bool:
    if not value or len(value) > _MAX_RELATIVE_PATH:
        return True
    if value.endswith("/") or "\\" in value:
        return True
    return _unnormalized_path_text(value)


def _encoded_relative_path(value: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PathContainmentError(
            "repository source path is not canonical"
        ) from exc


def _rooted_path_text(value: str) -> bool:
    windows = PureWindowsPath(value)
    if PurePosixPath(value).is_absolute():
        return True
    return bool(windows.drive or windows.root)


def _uncanonical_path_shape(value: str, parts: tuple[str, ...]) -> bool:
    if _rooted_path_text(value) or len(parts) > _MAX_COMPONENTS:
        return True
    return any(part in {"", ".", ".."} for part in parts)


def _oversized_component(component: str) -> bool:
    if len(component) > _MAX_COMPONENT_CHARACTERS:
        return True
    return len(component.encode("utf-8")) > _MAX_COMPONENT_BYTES


def _unsafe_path_component(component: str) -> bool:
    if _oversized_component(component):
        return True
    return _unportable_component_text(component)


def _unportable_component_text(component: str) -> bool:
    if component[-1] in {".", " "}:
        return True
    if any(character in '<>:"|?*' for character in component):
        return True
    return component.split(".", 1)[0].rstrip(" .").casefold() in _WINDOWS_RESERVED


def _require_safe_components(parts: tuple[str, ...]) -> None:
    for component in parts:
        if _unsafe_path_component(component):
            raise PathContainmentError(
                "repository source path contains an unsafe component"
            )


def _require_canonical_bounded_text(value: str) -> None:
    if _uncanonical_path_text(value):
        raise PathContainmentError("repository source path is not canonical")
    if len(_encoded_relative_path(value)) > _MAX_RELATIVE_PATH:
        raise PathContainmentError("repository source path exceeds its byte ceiling")


def _validate_relative_path(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise TypeError("relative_path must be a string")
    _require_canonical_bounded_text(value)
    parts = tuple(value.split("/"))
    if _uncanonical_path_shape(value, parts):
        raise PathContainmentError("repository source path is not canonical")
    _require_safe_components(parts)
    return value, parts


def validate_repository_relative_path(value: str) -> str:
    """Validate and return one canonical repository-relative path lexically."""
    normalized, _parts = _validate_relative_path(value)
    return normalized


def _require_repository(value: object) -> RepositoryScope:
    if not isinstance(value, RepositoryScope):
        raise TypeError("repository must be RepositoryScope")
    return value


def _posix_identity(info: os.stat_result) -> tuple[object, ...]:
    if info.st_dev < 0 or info.st_ino < 0:
        raise PathContainmentError("stable repository source identity is unavailable")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _posix_directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PathContainmentError("no-follow repository traversal is unavailable")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _posix_file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise PathContainmentError("no-follow repository traversal is unavailable")
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _assert_ancestry(root: Path, target: Path) -> None:
    try:
        target.relative_to(root)
        normalized_root = os.path.normcase(str(root))
        normalized_target = os.path.normcase(str(target))
        common = os.path.commonpath((normalized_root, normalized_target))
    except (OSError, ValueError) as exc:
        raise PathContainmentError("repository source ancestry is invalid") from exc
    if common != normalized_root:
        raise PathContainmentError("repository source ancestry is invalid")


def _open_posix_checkout(
    root: Path,
    owned: _OwnedHandles,
) -> tuple[int, tuple[object, ...], tuple[_TraversalStep, ...]]:
    directory_flags = _posix_directory_flags()
    current = owned.own(os.open("/", directory_flags))
    filesystem_root_identity = _posix_identity(os.fstat(current))
    steps: list[_TraversalStep] = []
    for component in root.parts[1:]:
        opened = owned.own(os.open(component, directory_flags, dir_fd=current))
        info = os.fstat(opened)
        if not stat.S_ISDIR(info.st_mode):
            raise PathContainmentError("repository checkout root is not a directory")
        steps.append(_TraversalStep(component, _posix_identity(info), True))
        current = opened
    return current, filesystem_root_identity, tuple(steps)


def _require_posix_root(descriptor: int, filesystem_root_identity: tuple[object, ...]) -> None:
    root_info = os.fstat(descriptor)
    if not stat.S_ISDIR(root_info.st_mode) or _posix_identity(root_info) != filesystem_root_identity:
        raise PathContainmentError("filesystem root changed during traversal")


def _reopen_posix_step(owned: _OwnedHandles, current: int, step: _TraversalStep) -> int:
    flags = _posix_directory_flags() if step.directory else _posix_file_flags()
    opened = owned.own(os.open(step.name, flags, dir_fd=current))
    info = os.fstat(opened)
    expected_kind = stat.S_ISDIR if step.directory else stat.S_ISREG
    if not expected_kind(info.st_mode) or _posix_identity(info) != step.identity:
        raise PathContainmentError("repository source changed during traversal")
    return opened


def _revalidate_posix(
    filesystem_root_identity: tuple[object, ...],
    root_steps: tuple[_TraversalStep, ...],
    source_steps: tuple[_TraversalStep, ...],
) -> None:
    with _OwnedHandles(os.close) as owned:
        current = owned.own(os.open("/", _posix_directory_flags()))
        _require_posix_root(current, filesystem_root_identity)
        for step in (*root_steps, *source_steps):
            current = _reopen_posix_step(owned, current, step)


class _PosixWalk:
    """The state one no-follow walk threads through its components."""

    def __init__(self, owned: _OwnedHandles, root_descriptor: int, must_exist: bool) -> None:
        self.owned = owned
        self.current = root_descriptor
        self.must_exist = must_exist
        self.steps: list[_TraversalStep] = []
        self.missing: tuple[str, ...] = ()
        self.final_descriptor: int | None = None

    def walk(self, parts: tuple[str, ...]) -> None:
        for index, component in enumerate(parts):
            if not self._open_component(component, final=index == len(parts) - 1):
                self.missing = parts[index:]
                return

    def _open_component(self, component: str, *, final: bool) -> bool:
        if final:
            return self._open_final(component)
        return self._open_parent(component)

    def _require_absence_allowed(self, message: str) -> None:
        if self.must_exist:
            raise PathContainmentError(message) from None

    def _open_final(self, component: str) -> bool:
        """Open the last component as a regular file; False when it is absent."""
        try:
            named = os.stat(component, dir_fd=self.current, follow_symlinks=False)
        except FileNotFoundError:
            self._require_absence_allowed("repository source does not exist")
            return False
        if not stat.S_ISREG(named.st_mode):
            raise PathContainmentError("repository source is not a regular file")
        opened = self.owned.own(os.open(component, _posix_file_flags(), dir_fd=self.current))
        info = os.fstat(opened)
        identity = _posix_identity(info)
        if not stat.S_ISREG(info.st_mode) or identity != _posix_identity(named):
            raise PathContainmentError("repository source changed before open")
        self.steps.append(_TraversalStep(component, identity, False))
        self.final_descriptor = opened
        return True

    def _open_parent(self, component: str) -> bool:
        """Open one directory component; False when it is absent."""
        try:
            opened = self.owned.own(
                os.open(component, _posix_directory_flags(), dir_fd=self.current)
            )
        except FileNotFoundError:
            self._require_absence_allowed("repository source parent does not exist")
            return False
        info = os.fstat(opened)
        if not stat.S_ISDIR(info.st_mode):
            raise PathContainmentError("repository source parent is not a directory")
        self.steps.append(_TraversalStep(component, _posix_identity(info), True))
        self.current = opened
        return True


def _require_posix_still_missing(walk: _PosixWalk) -> None:
    if not walk.missing:
        return
    try:
        os.stat(walk.missing[0], dir_fd=walk.current, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise PathContainmentError("repository source appeared during traversal")


def _read_posix_content(walk: _PosixWalk, reader: Callable[[int], bytes]) -> bytes:
    if walk.final_descriptor is None or walk.missing:
        raise PathContainmentError("repository source does not exist")
    content = reader(walk.final_descriptor)
    if _posix_identity(os.fstat(walk.final_descriptor)) != walk.steps[-1].identity:
        raise PathContainmentError("repository source changed during read")
    return content


def _repository_source(
    repository: RepositoryScope, relative_path: str, absolute_path: Path
) -> RepositorySource:
    return RepositorySource(
        repository.repository_id,
        repository.checkout_id,
        relative_path,
        absolute_path,
        path_to_file_uri(absolute_path),
    )


def _access_posix(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
    reader: Callable[[int], bytes] | None,
) -> tuple[RepositorySource, bytes | None]:
    root = Path(repository.checkout_root)
    if not root.is_absolute():
        raise PathContainmentError("repository checkout root is not local and absolute")
    with _OwnedHandles(os.close) as owned:
        root_descriptor, filesystem_root_identity, root_steps = _open_posix_checkout(
            root, owned
        )
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise PathContainmentError("repository checkout root is not a directory")
        walk = _PosixWalk(owned, root_descriptor, must_exist)
        walk.walk(parts)

        _resolution_barrier()
        step_tuple = tuple(walk.steps)
        _revalidate_posix(filesystem_root_identity, root_steps, step_tuple)
        _require_posix_still_missing(walk)
        content = None
        if reader is not None:
            content = _read_posix_content(walk, reader)
            _revalidate_posix(filesystem_root_identity, root_steps, step_tuple)
        return _repository_source(repository, relative_path, root.joinpath(*parts)), content


def _resolve_posix(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
) -> RepositorySource:
    return _access_posix(
        repository,
        relative_path,
        parts,
        must_exist=must_exist,
        reader=None,
    )[0]


def _require_valid_windows_entry(entry: object) -> None:
    if (
        not isinstance(entry, windows_workspace.WindowsEntry)
        or unicodedata.normalize("NFC", entry.name) != entry.name
        or entry.kind not in {"directory", "file", "link"}
    ):
        raise PathContainmentError("Windows repository enumeration is invalid")


def _windows_entries(handle: int) -> dict[str, windows_workspace.WindowsEntry]:
    entries = windows_workspace.list_directory(
        handle, max_entries=_MAX_DIRECTORY_ENTRIES
    )
    by_folded_name: dict[str, windows_workspace.WindowsEntry] = {}
    for entry in entries:
        _require_valid_windows_entry(entry)
        folded = entry.name.casefold()
        previous = by_folded_name.get(folded)
        if previous is not None and previous.name != entry.name:
            raise PathContainmentError("Windows repository contains a case collision")
        by_folded_name[folded] = entry
    return by_folded_name


def _windows_entry(
    handle: int, component: str
) -> windows_workspace.WindowsEntry | None:
    entry = _windows_entries(handle).get(component.casefold())
    if entry is not None and entry.name != component:
        raise PathContainmentError("Windows repository source uses a case alias")
    return entry


def _probe_windows_absence(
    parent: int, component: str, owned: _OwnedHandles
) -> tuple[bool, list[OSError], int]:
    """(opened by some opener, the other errors, how many openers found nothing)."""
    opened = False
    failures: list[OSError] = []
    missing = 0
    for opener in (
        windows_workspace.open_directory,
        windows_workspace.open_shared_readonly_source_file,
    ):
        try:
            owned.own(opener(parent, component))
        except FileNotFoundError:
            missing += 1
        except OSError as exc:
            failures.append(exc)
        else:
            opened = True
    return opened, failures, missing


def _prove_windows_component_missing(
    parent: int,
    component: str,
    owned: _OwnedHandles,
) -> None:
    opened, failures, missing = _probe_windows_absence(parent, component, owned)
    if opened:
        raise PathContainmentError("Windows repository source uses an unenumerated alias")
    if failures or missing != 2:
        raise PathContainmentError(
            "Windows repository source absence cannot be proven"
        ) from (failures[0] if failures else None)


def _windows_identity(handle: int, *, directory: bool) -> tuple[object, ...]:
    volume, file_id, actual_directory = windows_workspace.identity(
        handle, directory=directory
    )
    if actual_directory != directory or not isinstance(file_id, bytes) or not any(file_id):
        raise PathContainmentError("stable Windows repository identity is unavailable")
    return volume, file_id, actual_directory


def _open_windows_step(
    parent: int,
    entry: windows_workspace.WindowsEntry,
    component: str,
    *,
    directory: bool,
    owned: _OwnedHandles,
) -> tuple[int, tuple[object, ...]]:
    expected_kind = "directory" if directory else "file"
    if entry.kind != expected_kind:
        raise PathContainmentError("Windows repository source has the wrong kind")
    opener = (
        windows_workspace.open_directory
        if directory
        else windows_workspace.open_shared_readonly_source_file
    )
    opened = owned.own(opener(parent, component))
    identity = _windows_identity(opened, directory=directory)
    if identity[1] != entry.file_id:
        raise PathContainmentError("Windows repository source changed before open")
    return opened, identity


def _require_windows_file_step(entry: windows_workspace.WindowsEntry, step: _TraversalStep) -> None:
    if entry.kind != "file" or entry.file_id != step.identity[1]:
        raise PathContainmentError("Windows repository source changed during traversal")


def _revalidate_windows_step(current: int, step: _TraversalStep, owned: _OwnedHandles) -> int:
    """Re-check one recorded step; the directory handle to continue from."""
    entry = _windows_entry(current, step.name)
    if entry is None:
        raise PathContainmentError("Windows repository source changed during traversal")
    if not step.directory:
        _require_windows_file_step(entry, step)
        return current
    return _reopened_windows_directory(current, entry, step, owned)


def _reopened_windows_directory(
    current: int, entry: object, step: _TraversalStep, owned: _OwnedHandles
) -> int:
    opened, identity = _open_windows_step(
        current, entry, step.name, directory=True, owned=owned
    )
    if identity != step.identity:
        raise PathContainmentError("Windows repository source changed during traversal")
    return opened


def _revalidate_windows(
    root: Path,
    root_identity: tuple[object, ...],
    steps: tuple[_TraversalStep, ...],
    owned: _OwnedHandles,
) -> None:
    current = owned.own(windows_workspace.open_directory_path(root))
    if _windows_identity(current, directory=True) != root_identity:
        raise PathContainmentError("repository checkout changed during traversal")
    for step in steps:
        current = _revalidate_windows_step(current, step, owned)


class _WindowsWalk:
    """The state one no-follow walk threads through its components."""

    def __init__(self, owned: _OwnedHandles, root_handle: int, must_exist: bool) -> None:
        self.owned = owned
        self.current = root_handle
        self.must_exist = must_exist
        self.steps: list[_TraversalStep] = []
        self.missing: tuple[str, ...] = ()
        self.final_handle: int | None = None

    def walk(self, parts: tuple[str, ...]) -> None:
        for index, component in enumerate(parts):
            final = index == len(parts) - 1
            entry = _windows_entry(self.current, component)
            if entry is None:
                self._note_missing(parts, index, component)
                return
            self._open(entry, component, final)

    def _note_missing(self, parts: tuple[str, ...], index: int, component: str) -> None:
        if self.must_exist:
            raise PathContainmentError("repository source does not exist")
        _prove_windows_component_missing(self.current, component, self.owned)
        self.missing = parts[index:]

    def _open(self, entry: windows_workspace.WindowsEntry, component: str, final: bool) -> None:
        opened, identity = _open_windows_step(
            self.current, entry, component, directory=not final, owned=self.owned
        )
        self.steps.append(_TraversalStep(component, identity, not final))
        if final:
            self.final_handle = opened
            return
        self.current = opened


def _existing_directory_steps(steps: list[_TraversalStep]) -> tuple[_TraversalStep, ...]:
    return tuple(step for step in steps if step.directory)


def _expected_parent_identity(
    existing: tuple[_TraversalStep, ...], root_identity: tuple[object, ...]
) -> tuple[object, ...]:
    if existing:
        return existing[-1].identity
    return root_identity


def _windows_parent_probe(
    canonical_root: Path,
    root_identity: tuple[object, ...],
    steps: list[_TraversalStep],
    owned: _OwnedHandles,
) -> tuple[Path, int]:
    """(canonical path, handle) of the nearest directory the walk proved exists."""
    existing = _existing_directory_steps(steps)
    nearest_path = canonical_root.joinpath(*(step.name for step in existing))
    canonical_parent = nearest_path.resolve(strict=True)
    _assert_ancestry(canonical_root, canonical_parent)
    parent_probe = owned.own(windows_workspace.open_directory_path(canonical_parent))
    expected_parent = _expected_parent_identity(existing, root_identity)
    if _windows_identity(parent_probe, directory=True) != expected_parent:
        raise PathContainmentError("repository source parent changed")
    return canonical_parent, parent_probe


def _windows_missing_path(
    parent_probe: int, canonical_parent: Path, missing: tuple[str, ...], owned: _OwnedHandles
) -> Path:
    if _windows_entry(parent_probe, missing[0]) is not None:
        raise PathContainmentError("repository source appeared during traversal")
    _prove_windows_component_missing(parent_probe, missing[0], owned)
    return canonical_parent.joinpath(*missing)


def _windows_final_matches(
    walk: _WindowsWalk, final_entry: windows_workspace.WindowsEntry | None
) -> bool:
    if walk.final_handle is None or final_entry is None:
        return False
    if final_entry.kind != "file" or final_entry.file_id != walk.steps[-1].identity[1]:
        return False
    return _windows_identity(walk.final_handle, directory=False) == walk.steps[-1].identity


def _windows_existing_path(
    canonical_root: Path, parts: tuple[str, ...], walk: _WindowsWalk, owned: _OwnedHandles
) -> Path:
    absolute_path = canonical_root.joinpath(*parts).resolve(strict=True)
    _assert_ancestry(canonical_root, absolute_path)
    final_parent = owned.own(windows_workspace.open_directory_path(absolute_path.parent))
    final_entry = _windows_entry(final_parent, absolute_path.name)
    if not _windows_final_matches(walk, final_entry):
        raise PathContainmentError("repository source changed")
    return absolute_path


def _read_windows_content(walk: _WindowsWalk, reader: Callable[[int], bytes]) -> bytes:
    if walk.final_handle is None or walk.missing:
        raise PathContainmentError("repository source does not exist")
    content = reader(walk.final_handle)
    if _windows_identity(walk.final_handle, directory=False) != walk.steps[-1].identity:
        raise PathContainmentError("repository source changed during read")
    return content


def _require_windows_local_root(checkout_root: object) -> None:
    pure_root = PureWindowsPath(checkout_root)
    if not pure_root.drive or not pure_root.root or pure_root.drive.startswith("\\"):
        raise PathContainmentError("repository checkout root is not a local drive path")


def _open_windows_checkout(
    root: Path, owned: _OwnedHandles
) -> tuple[int, tuple[object, ...], Path]:
    """(root handle, its identity, the canonical root) after both open the same directory."""
    root_handle = owned.own(windows_workspace.open_directory_path(root))
    root_identity = _windows_identity(root_handle, directory=True)
    canonical_root = root.resolve(strict=True)
    canonical_handle = owned.own(windows_workspace.open_directory_path(canonical_root))
    if _windows_identity(canonical_handle, directory=True) != root_identity:
        raise PathContainmentError("repository checkout root changed")
    return root_handle, root_identity, canonical_root


def _windows_absolute_path(
    canonical_root: Path,
    parts: tuple[str, ...],
    walk: _WindowsWalk,
    canonical_parent: Path,
    parent_probe: int,
    owned: _OwnedHandles,
) -> Path:
    if walk.missing:
        return _windows_missing_path(parent_probe, canonical_parent, walk.missing, owned)
    return _windows_existing_path(canonical_root, parts, walk, owned)


def _access_windows(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
    reader: Callable[[int], bytes] | None,
) -> tuple[RepositorySource, bytes | None]:
    root = Path(repository.checkout_root)
    _require_windows_local_root(repository.checkout_root)
    with _OwnedHandles(windows_workspace.close_handle) as owned:
        root_handle, root_identity, canonical_root = _open_windows_checkout(root, owned)
        walk = _WindowsWalk(owned, root_handle, must_exist)
        walk.walk(parts)

        _resolution_barrier()
        step_tuple = tuple(walk.steps)
        _revalidate_windows(canonical_root, root_identity, step_tuple, owned)
        canonical_parent, parent_probe = _windows_parent_probe(
            canonical_root, root_identity, walk.steps, owned
        )
        absolute_path = _windows_absolute_path(
            canonical_root, parts, walk, canonical_parent, parent_probe, owned
        )
        content = None
        if reader is not None:
            content = _read_windows_content(walk, reader)
            _revalidate_windows(canonical_root, root_identity, step_tuple, owned)
        return _repository_source(repository, relative_path, absolute_path), content


def _resolve_windows(
    repository: RepositoryScope,
    relative_path: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
) -> RepositorySource:
    return _access_windows(
        repository,
        relative_path,
        parts,
        must_exist=must_exist,
        reader=None,
    )[0]


def _platform_access(
    repository: RepositoryScope,
    normalized: str,
    parts: tuple[str, ...],
    *,
    must_exist: bool,
    reader: Callable[[int], bytes] | None,
) -> tuple[RepositorySource, bytes | None]:
    if os.name == "posix":
        return _access_posix(repository, normalized, parts, must_exist=must_exist, reader=reader)
    if os.name == "nt":
        return _access_windows(repository, normalized, parts, must_exist=must_exist, reader=reader)
    raise PathContainmentError("no-follow repository traversal is unavailable")


def _contained(action: Callable[[], object], *, passthrough: tuple[type[BaseException], ...]):
    """Run one containment action; every other failure becomes a containment error."""
    try:
        return action()
    except passthrough:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise PathContainmentError("repository source containment failed") from exc


def resolve_repository_source(
    repository: RepositoryScope,
    relative_path: str,
    *,
    must_exist: bool = True,
) -> RepositorySource:
    """Resolve one canonical repository-relative source through a no-follow walk."""
    repository = _require_repository(repository)
    if not isinstance(must_exist, bool):
        raise TypeError("must_exist must be a boolean")
    normalized, parts = _validate_relative_path(relative_path)
    return _contained(
        lambda: _platform_access(
            repository, normalized, parts, must_exist=must_exist, reader=None
        )[0],
        passthrough=(PathContainmentError,),
    )


def _validated_source_read_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return _finite_source_read_deadline(deadline)


def _finite_source_read_deadline(deadline: object) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp or None")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _check_source_read_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("repository source read deadline expired")


def _posix_read_identity(info: os.stat_result) -> tuple[object, ...]:
    return (_posix_identity(info), info.st_size, info.st_mtime_ns)


def _read_bounded_chunks(descriptor: int, max_bytes: int, deadline: float | None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        _check_source_read_deadline(deadline)
        chunk = os.read(descriptor, min(_SOURCE_READ_CHUNK_BYTES, max_bytes + 1 - total))
        _check_source_read_deadline(deadline)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise PathContainmentError("repository source exceeds its byte ceiling")
    return content


def _require_bounded_regular_source(before: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(before.st_mode):
        raise PathContainmentError("repository source is not a regular file")
    if before.st_size > max_bytes:
        raise PathContainmentError("repository source exceeds its byte ceiling")


def _read_posix_source_handle(
    descriptor: int,
    *,
    max_bytes: int,
    deadline: float | None,
) -> bytes:
    _check_source_read_deadline(deadline)
    before = os.fstat(descriptor)
    _require_bounded_regular_source(before, max_bytes)
    content = _read_bounded_chunks(descriptor, max_bytes, deadline)
    if _posix_read_identity(before) != _posix_read_identity(os.fstat(descriptor)):
        raise PathContainmentError("repository source changed during read")
    return content


def _windows_read_identity(handle: int) -> tuple[object, ...]:
    return (
        _windows_identity(handle, directory=False),
        windows_workspace.file_size(handle),
        windows_workspace.file_modified_time_ns(handle),
    )


def _read_windows_chunks(handle: int, max_bytes: int, deadline: float | None) -> bytes:
    windows_workspace.seek_start(handle)
    chunks = []
    for chunk in windows_workspace.read_chunks(
        handle, chunk_bytes=_SOURCE_READ_CHUNK_BYTES, max_bytes=max_bytes
    ):
        _check_source_read_deadline(deadline)
        chunks.append(chunk)
    _check_source_read_deadline(deadline)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise PathContainmentError("repository source exceeds its byte ceiling")
    return content


def _read_windows_source_handle(
    handle: int,
    *,
    max_bytes: int,
    deadline: float | None,
) -> bytes:
    _check_source_read_deadline(deadline)
    before = _windows_read_identity(handle)
    if before[1] > max_bytes:
        raise PathContainmentError("repository source exceeds its byte ceiling")
    content = _read_windows_chunks(handle, max_bytes, deadline)
    if _windows_read_identity(handle) != before:
        raise PathContainmentError("repository source changed during read")
    return content


def _require_byte_ceiling(max_bytes: object) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _bounded_source_reader(max_bytes: int, deadline: float | None) -> Callable[[int], bytes]:
    if os.name == "nt":
        return lambda handle: _read_windows_source_handle(
            handle, max_bytes=max_bytes, deadline=deadline
        )
    return lambda descriptor: _read_posix_source_handle(
        descriptor, max_bytes=max_bytes, deadline=deadline
    )


def read_repository_source_bytes(
    repository: RepositoryScope,
    relative_path: str,
    *,
    max_bytes: int,
    deadline: float | None = None,
) -> bytes:
    """Read one bounded source through the final retained containment handle."""
    repository = _require_repository(repository)
    _require_byte_ceiling(max_bytes)
    deadline = _validated_source_read_deadline(deadline)
    normalized, parts = _validate_relative_path(relative_path)
    _check_source_read_deadline(deadline)
    reader = _bounded_source_reader(max_bytes, deadline)
    _source, content = _contained(
        lambda: _platform_access(repository, normalized, parts, must_exist=True, reader=reader),
        passthrough=(TimeoutError, PathContainmentError),
    )
    _check_source_read_deadline(deadline)
    if content is None:
        raise PathContainmentError("repository source read did not complete")
    return content


def _unsafe_uri_shape(uri: object) -> bool:
    if not isinstance(uri, str) or not uri:
        return True
    return len(uri) > _MAX_PROVIDER_URI


def _outside_printable_ascii(character: str) -> bool:
    return ord(character) > 127 or _is_control(character) or character.isspace()


def _unsafe_uri_characters(uri: str) -> bool:
    if any(_outside_printable_ascii(character) for character in uri):
        return True
    return any(character in uri for character in ("\\", "?", "#"))


def _unsafe_provider_uri_text(uri: object) -> bool:
    """Refuse anything the URI grammar allows but our path rules do not."""
    if _unsafe_uri_shape(uri):
        return True
    if _unsafe_uri_characters(uri):
        return True
    return bool(_MALFORMED_PERCENT.search(uri) or _ENCODED_SEPARATOR.search(uri))


def _split_provider_uri(uri: str) -> tuple[object, str, str] | None:
    try:
        parsed = urlsplit(uri)
        authority = unquote_to_bytes(parsed.netloc).decode("utf-8", errors="strict")
        path = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    return parsed, authority, path


def _unsafe_provider_scheme(parsed: object) -> bool:
    if parsed.scheme.casefold() != "file":
        return True
    return bool(parsed.query or parsed.fragment)


def _unsafe_provider_authority(authority: str) -> bool:
    if authority.casefold() not in {"", "localhost"}:
        return True
    return "@" in authority


def _unsafe_provider_path(path: str) -> bool:
    return "\\" in path or path.startswith("//")


def _unsafe_provider_parts(parsed: object, authority: str, path: str) -> bool:
    if _unsafe_provider_scheme(parsed) or _unsafe_provider_authority(authority):
        return True
    if any(_is_control(character) for character in authority + path):
        return True
    return _unsafe_provider_path(path)


def _windows_path_components(path: str) -> list[str] | None:
    local = path[1:] if path.startswith("/") else path
    if not _WINDOWS_DRIVE_PATH.match(local):
        return None
    if len(local) > 3:
        return local[3:].split("/")
    return []


def _posix_path_components(path: str) -> list[str] | None:
    if not path.startswith("/") or path.startswith("//"):
        return None
    if len(path) > 1:
        return path[1:].split("/")
    return []


def _provider_path_components(path: str) -> list[str] | None:
    if os.name == "nt":
        return _windows_path_components(path)
    if os.name == "posix":
        return _posix_path_components(path)
    return None


def _traversing_components(components: list[str]) -> bool:
    return any(component in {"", ".", ".."} for component in components)


def _validated_provider_parts(uri: object) -> tuple[str, str] | None:
    if _unsafe_provider_uri_text(uri):
        return None
    return _safe_provider_parts(_split_provider_uri(uri))


def _safe_provider_parts(split: tuple | None) -> tuple[str, str] | None:
    if split is None:
        return None
    parsed, authority, path = split
    if _unsafe_provider_parts(parsed, authority, path):
        return None
    return authority, path


def _decoded_provider_uri(uri: object) -> tuple[str, str] | None:
    parts = _validated_provider_parts(uri)
    if parts is None:
        return None
    authority, path = parts
    components = _provider_path_components(path)
    if components is None or _traversing_components(components):
        return None
    return authority, path


def _windows_shares_root(provider: PureWindowsPath, root: PureWindowsPath) -> bool:
    if len(provider.parts) < len(root.parts):
        return False
    if provider.parts[0].casefold() != root.parts[0].casefold():
        return False
    return provider.parts[1 : len(root.parts)] == root.parts[1:]


def _windows_provider_in_root(
    provider_path: PurePath, checkout_root: object
) -> tuple[PurePath, PurePath] | None:
    provider = PureWindowsPath(provider_path)
    root = PureWindowsPath(checkout_root)
    if provider.drive.startswith("\\") or not provider.is_absolute():
        return None
    if not _windows_shares_root(provider, root):
        return None
    return provider, root


def _provider_path_in_root(
    provider_path: PurePath, checkout_root: object
) -> tuple[PurePath, PurePath] | None:
    """(provider path, checkout root) as pure paths, or None when the path is outside."""
    if os.name == "nt":
        return _windows_provider_in_root(provider_path, checkout_root)
    provider = PurePosixPath(provider_path)
    root = PurePosixPath(checkout_root)
    if not provider.is_absolute() or str(provider).startswith("//"):
        return None
    return provider, root


def normalize_provider_uri(
    repository: RepositoryScope,
    uri: str,
) -> RepositorySource | None:
    """Return one canonical in-repository provider location or filter it."""
    repository = _require_repository(repository)
    if _decoded_provider_uri(uri) is None:
        return None
    try:
        pair = _provider_path_in_root(
            file_uri_to_path(uri, platform=os.name), repository.checkout_root
        )
        if pair is None:
            return None
        provider, root = pair
        normalized, _parts = _validate_relative_path(provider.relative_to(root).as_posix())
        return resolve_repository_source(repository, normalized)
    except Exception:
        return None


def _quoted_value_end(value: str, value_start: int) -> int:
    """The index after the closing quote (or the end of the text)."""
    quote = value[value_start]
    value_end = value_start + 1
    escaped = False
    while value_end < len(value):
        character = value[value_end]
        value_end += 1
        if character == quote and not escaped:
            break
        escaped = character == "\\" and not escaped
    return value_end


def _line_value_end(value: str, value_end: int) -> int:
    while value_end < len(value) and value[value_end] not in "\r\n":
        value_end += 1
    return value_end


def _assignment_value_end(value: str, value_start: int) -> int:
    if value_start < len(value) and value[value_start] in {'"', "'"}:
        return _quoted_value_end(value, value_start)
    return _line_value_end(value, value_start)


def _redact_assignments(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _CREDENTIAL_ASSIGNMENT.finditer(value):
        if match.start() < cursor:
            continue
        pieces.append(value[cursor : match.end()])
        pieces.append("<redacted>")
        cursor = _assignment_value_end(value, match.end())
    pieces.append(value[cursor:])
    return "".join(pieces)


def _scheme_character(character: str) -> bool:
    return character.isascii() and (
        character.isalpha() or character.isdigit() or character in "+.-"
    )


def _scheme_start(value: str, scheme_end: int) -> int:
    scheme_start = scheme_end
    while scheme_start > 0 and _scheme_character(value[scheme_start - 1]):
        scheme_start -= 1
    return scheme_start


def _is_scheme(value: str, scheme_start: int, scheme_end: int) -> bool:
    if scheme_start >= scheme_end:
        return False
    return value[scheme_start].isascii() and value[scheme_start].isalpha()


def _authority_terminator(character: str) -> bool:
    if character in "/?#" or character in _URL_AUTHORITY_TOKEN_BOUNDARIES:
        return True
    return character.isspace() or _is_control(character)


def _authority_end(value: str, authority_start: int) -> int:
    authority_end = authority_start
    while authority_end < len(value) and not _authority_terminator(value[authority_end]):
        authority_end += 1
    return authority_end


def _userinfo_span(value: str, scheme_end: int) -> tuple[int, int] | None:
    """(authority start, index of the last `@`) of a URL with userinfo, else None."""
    if not _is_scheme(value, _scheme_start(value, scheme_end), scheme_end):
        return None
    authority_start = scheme_end + 3
    userinfo_end = value.rfind("@", authority_start, _authority_end(value, authority_start))
    if userinfo_end < authority_start:
        return None
    return authority_start, userinfo_end


def _redact_url_userinfo(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    search_start = 0
    while True:
        scheme_end = value.find("://", search_start)
        if scheme_end < 0:
            break
        search_start = scheme_end + 3
        span = _userinfo_span(value, scheme_end)
        if span is None or span[0] < cursor:
            continue
        pieces.append(value[cursor : span[0]])
        pieces.append("<redacted>@")
        cursor = span[1] + 1
    pieces.append(value[cursor:])
    return "".join(pieces)


def _localhost_uri_remainder(stripped: str, has_leading_separator: bool) -> str | None:
    authority, separator, remainder = stripped.partition("\\")
    if not has_leading_separator or not separator or authority.casefold() != "localhost":
        return None
    return remainder.lstrip("\\")


def _windows_uri_candidate(candidate: str) -> str | None:
    """The drive path a `file:` URI names, or None when it names anything else."""
    if candidate[:5].casefold() != "file:":
        return None
    uri_path = candidate[5:].replace("/", "\\")
    has_leading_separator = uri_path.startswith("\\")
    stripped = uri_path.lstrip("\\")
    if re.match(r"(?i)^[A-Z]:\\", stripped):
        return stripped
    return _localhost_uri_remainder(stripped, has_leading_separator)


def _windows_candidate_text(value: str, file_uri: bool) -> str | None:
    if file_uri:
        return _windows_uri_candidate(value)
    return value.replace("/", "\\")


def _windows_drive_and_tail(candidate: str) -> tuple[str, str] | None:
    if candidate.startswith("\\"):
        return None
    drive, tail = ntpath.splitdrive(candidate)
    if not re.fullmatch(r"(?i)[A-Z]:", drive) or not tail.startswith("\\"):
        return None
    return drive, tail


def _windows_component_is_valid(component: str) -> bool:
    if len(component) > _MAX_COMPONENT_CHARACTERS:
        return False
    return not any(character in '<>:"|?*' or _is_control(character) for character in component)


def _kept_windows_component(component: str) -> str | None:
    """The component as kept; "" when it trims to nothing; None when it is refused."""
    if component in {".", ".."}:
        return component
    return _trimmed_windows_component(component.rstrip(" ."))


def _trimmed_windows_component(component: str) -> str | None:
    """A component already stripped of trailing dots and spaces, as kept."""
    if not component:
        return ""
    if not _windows_component_is_valid(component):
        return None
    return component


def _raw_windows_components(tail: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"\\+", tail) if part)


def _windows_components(tail: str) -> list[str] | None:
    components: list[str] = []
    for component in _raw_windows_components(tail):
        kept = _kept_windows_component(component)
        if kept is None:
            return None
        if kept:
            components.append(kept)
    return components


def _folded_components(tail: str) -> tuple[str, ...]:
    return tuple(component.casefold() for component in tail.split("\\") if component)


def _has_dot_components(components: tuple[str, ...]) -> bool:
    return any(component in {".", ".."} for component in components)


def _normalized_windows_token(
    drive: str, components: list[str]
) -> tuple[str, tuple[str, ...]] | None:
    normalized = ntpath.normpath(drive + "\\" + "\\".join(components))
    normalized_drive, normalized_tail = ntpath.splitdrive(normalized)
    if not re.fullmatch(r"(?i)[A-Z]:", normalized_drive):
        return None
    normalized_components = _folded_components(normalized_tail)
    if len(normalized_components) > _MAX_COMPONENTS or _has_dot_components(normalized_components):
        return None
    return normalized_drive.casefold(), normalized_components


def _canonical_windows_path_token(
    value: str,
    *,
    file_uri: bool,
) -> tuple[str, tuple[str, ...]] | None:
    candidate = _windows_candidate_text(value, file_uri)
    if candidate is None:
        return None
    return _windows_candidate_token(candidate)


def _windows_candidate_token(candidate: str) -> tuple[str, tuple[str, ...]] | None:
    drive_tail = _windows_drive_and_tail(candidate)
    if drive_tail is None:
        return None
    components = _windows_components(drive_tail[1])
    if components is None:
        return None
    return _normalized_windows_token(drive_tail[0], components)


def _short_path_token(path: Path) -> tuple[str, tuple[str, ...]] | None:
    try:
        short_path = windows_workspace.get_short_path(path)
    except (OSError, RuntimeError, ValueError):
        return None
    return _canonical_windows_path_token(str(short_path), file_uri=False)


def _add_short_aliases(
    aliases: list[set[str]], drive: str, short: tuple[str, tuple[str, ...]] | None
) -> None:
    if short is None or short[0] != drive or len(short[1]) != len(aliases):
        return
    for component_aliases, short_component in zip(aliases, short[1]):
        component_aliases.add(short_component)


def _windows_root_component_aliases(
    path: Path,
) -> tuple[str, tuple[frozenset[str], ...]] | None:
    canonical = _canonical_windows_path_token(str(path), file_uri=False)
    if canonical is None:
        return None
    drive, components = canonical
    aliases = [{component} for component in components]
    _add_short_aliases(aliases, drive, _short_path_token(path))
    return drive, tuple(frozenset(component_aliases) for component_aliases in aliases)


def _windows_components_reach_root(
    drive: str,
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
) -> bool:
    root_drive, root_component_aliases = root
    return (
        drive == root_drive
        and len(components) == len(root_component_aliases)
        and all(
            component in aliases
            for component, aliases in zip(components, root_component_aliases)
        )
    )


def _windows_candidate_inspection_characters(
    root: tuple[str, tuple[frozenset[str], ...]],
) -> int:
    _drive, aliases = root
    root_characters = 3 + sum(
        max((len(alias) for alias in component_aliases), default=0) + 1
        for component_aliases in aliases
    )
    return min(
        _MAX_REDACTION_PATH_TOKEN,
        _MAX_REDACTION_PATH_TOKEN // 2 + root_characters * 12,
    )


def _drive_spec_at(value: str, index: int) -> bool:
    """A drive letter, a colon and a separator start at `index`."""
    letter = value[index : index + 1]
    if not letter.isascii() or not letter.isalpha():
        return False
    return value[index + 1 : index + 2] == ":" and value[index + 2 : index + 3] in {"/", "\\"}


def _windows_candidate_starts_at(value: str, start: int) -> bool:
    if value[start : start + 5].casefold() == "file:":
        return True
    if _windows_extended_local_start(value, start) is not None:
        return True
    return _drive_spec_at(value, start)


def _windows_extended_local_start(value: str, start: int) -> int | None:
    if value[start : start + 4] not in {"\\\\?\\", "//?/"}:
        return None
    drive_start = start + 4
    if not _drive_spec_at(value, drive_start):
        return None
    return drive_start


def _windows_hard_boundary(character: str) -> bool:
    if character in {"/", "\\", ":"} or character.isspace():
        return True
    return _is_control(character) or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS


def _windows_boundary_character(character: str, quoted: bool) -> bool:
    if _windows_hard_boundary(character):
        return True
    return quoted and character == '"'


def _trailing_punctuation_end(value: str, index: int) -> int:
    while index < len(value) and value[index] in _WINDOWS_LOG_TRAILING_PUNCTUATION:
        index += 1
    return index


def _after_punctuation_is_boundary(value: str, punctuation_end: int) -> bool:
    if punctuation_end >= len(value):
        return True
    following = value[punctuation_end]
    return following.isspace() or _is_control(following) or following == '"'


def _punctuation_boundary(value: str, index: int) -> bool:
    """Log punctuation after the root ends it only when a boundary or a new token follows."""
    character = value[index]
    if character not in _WINDOWS_LOG_TRAILING_PUNCTUATION:
        return False
    punctuation_end = _trailing_punctuation_end(value, index)
    if _after_punctuation_is_boundary(value, punctuation_end):
        return True
    return character in {",", ";"} and _windows_candidate_starts_at(value, punctuation_end)


def _windows_root_boundary(value: str, index: int, *, quoted: bool) -> bool:
    if index >= len(value):
        return True
    if _windows_boundary_character(value[index], quoted):
        return True
    return _punctuation_boundary(value, index)


def _windows_casefolded_prefix_end(
    value: str,
    start: int,
    folded_alias: str,
) -> int | None:
    folded = ""
    index = start
    while index < len(value) and len(folded) < len(folded_alias):
        folded += value[index].casefold()
        index += 1
        if not folded_alias.startswith(folded):
            return None
    return index if folded == folded_alias else None


def _skip_native_separators(value: str, index: int) -> int:
    while value[index : index + 1] in {"/", "\\"}:
        index += 1
    return index


def _longest_alias_end(value: str, index: int, aliases: frozenset[str]) -> int | None:
    for alias in sorted(aliases, key=len, reverse=True):
        alias_end = _windows_casefolded_prefix_end(value, index, alias)
        if alias_end is not None:
            return alias_end
    return None


def _absorb_trailing_dots_and_spaces(value: str, component_end: int) -> int:
    """Trailing dots and spaces before a separator belong to the component."""
    trailing_end = component_end
    while value[trailing_end : trailing_end + 1] in {".", " "}:
        trailing_end += 1
    if trailing_end > component_end and value[trailing_end : trailing_end + 1] in {"/", "\\"}:
        return trailing_end
    return component_end


def _dots_may_end_here(following: str) -> bool:
    if not following or following in {"/", "\\", '"'}:
        return True
    return following.isspace() or _is_control(following)


def _absorb_final_dots(value: str, component_end: int) -> int:
    dots_end = component_end
    while value[dots_end : dots_end + 1] == ".":
        dots_end += 1
    if dots_end == component_end:
        return component_end
    if _dots_may_end_here(value[dots_end : dots_end + 1]):
        return dots_end
    return component_end


def _native_component_end(value: str, index: int, aliases: frozenset[str]) -> int | None:
    component_end = _longest_alias_end(value, index, aliases)
    if component_end is None:
        return None
    return _absorb_trailing_dots_and_spaces(value, component_end)


def _next_native_component_start(value: str, component_end: int) -> int | None:
    if value[component_end : component_end + 1] not in {"/", "\\"}:
        return None
    return _skip_native_separators(value, component_end)


def _native_final_end(value: str, component_end: int, quoted: bool) -> int | None:
    component_end = _absorb_final_dots(value, component_end)
    if _windows_root_boundary(value, component_end, quoted=quoted):
        return component_end
    return None


def _match_native_components(
    value: str, index: int, root_component_aliases: tuple[frozenset[str], ...], quoted: bool
) -> int | None:
    if not root_component_aliases:
        return index
    index = _native_leading_components_end(value, index, root_component_aliases[:-1])
    if index is None:
        return None
    return _native_last_component_end(value, index, root_component_aliases[-1], quoted)


def _native_leading_components_end(
    value: str, index: int, leading: tuple[frozenset[str], ...]
) -> int | None:
    """Where the last root component starts, after every leading one matched."""
    for aliases in leading:
        index = _native_next_component_start(value, index, aliases)
        if index is None:
            return None
    return index


def _native_next_component_start(value: str, index: int, aliases: frozenset[str]) -> int | None:
    component_end = _native_component_end(value, index, aliases)
    if component_end is None:
        return None
    return _next_native_component_start(value, component_end)


def _native_last_component_end(
    value: str, index: int, aliases: frozenset[str], quoted: bool
) -> int | None:
    component_end = _native_component_end(value, index, aliases)
    if component_end is None:
        return None
    return _native_final_end(value, component_end, quoted)


def _windows_native_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[frozenset[str], ...]],
    *,
    quoted: bool,
) -> int | None:
    root_drive, root_component_aliases = root
    if value[start : start + 2].casefold() != root_drive:
        return None
    index = start + 2
    if value[index : index + 1] not in {"/", "\\"}:
        return None
    index = _skip_native_separators(value, index)
    return _match_native_components(value, index, root_component_aliases, quoted)


def _percent_byte(value: str, index: int, limit: int) -> int | None:
    """The byte a `%XX` at `index` encodes, or None when there is no such escape."""
    if index + 3 > limit or value[index] != "%":
        return None
    if re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3]) is None:
        return None
    return int(value[index + 1 : index + 3], 16)


# (lowest, highest, sequence length) for each well-formed multi-byte lead byte.
_UTF8_MULTIBYTE_LEADS = ((0xC2, 0xDF, 2), (0xE0, 0xEF, 3), (0xF0, 0xF4, 4))


def _utf8_sequence_length(first_byte: int) -> int | None:
    if first_byte < 0x80:
        return 1
    return next(
        (length for low, high, length in _UTF8_MULTIBYTE_LEADS if low <= first_byte <= high),
        None,
    )


def _percent_bytes(value: str, index: int, limit: int, count: int) -> tuple[bytes, int] | None:
    raw = bytearray()
    source_end = index
    for _byte_index in range(count):
        byte = _percent_byte(value, source_end, limit)
        if byte is None:
            return None
        raw.append(byte)
        source_end += 3
    return bytes(raw), source_end


def _decoded_single_character(raw: bytes) -> str | None:
    try:
        character = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if len(character) != 1:
        return None
    return character


def _percent_encoded_character(value: str, index: int, limit: int) -> tuple[str, int] | None:
    """(character, index after its escapes) for one percent-encoded code point."""
    first_byte = _percent_byte(value, index, limit)
    if first_byte is None:
        return None
    return _percent_sequence_character(value, index, limit, first_byte)


def _percent_sequence_character(
    value: str, index: int, limit: int, first_byte: int
) -> tuple[str, int] | None:
    count = _utf8_sequence_length(first_byte)
    if count is None:
        return None
    return _single_percent_character(_percent_bytes(value, index, limit, count))


def _single_percent_character(decoded: tuple | None) -> tuple[str, int] | None:
    if decoded is None:
        return None
    character = _decoded_single_character(decoded[0])
    if character is None:
        return None
    return character, decoded[1]


def _uri_character(
    value: str,
    index: int,
    limit: int,
) -> tuple[str, int, bool] | None:
    if index >= limit:
        return None
    if value[index] != "%":
        return value[index], index + 1, False
    return _encoded_uri_character(value, index, limit)


def _encoded_uri_character(value: str, index: int, limit: int) -> tuple[str, int, bool] | None:
    decoded = _percent_encoded_character(value, index, limit)
    if decoded is None:
        return None
    return decoded[0], decoded[1], True


def _skip_uri_separators(value: str, index: int, limit: int) -> tuple[int, int]:
    """(index after the run, how many separators it held)."""
    separators = 0
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None or decoded[0] not in {"/", "\\"}:
            return index, separators
        separators += 1
        index = decoded[1]
    return index, separators


def _uri_drive_pair(value: str, index: int, limit: int) -> tuple | None:
    """The two decoded characters that could spell a drive letter and colon."""
    first = _uri_character(value, index, limit)
    if first is None:
        return None
    second = _uri_character(value, first[1], limit)
    if second is None:
        return None
    return first, second


def _names_windows_drive(pair: tuple | None) -> bool:
    if pair is None:
        return False
    first, second = pair
    if not first[0].isascii() or not first[0].isalpha():
        return False
    return second[0] == ":"


def _authority_character_is_invalid(character: str) -> bool:
    return _is_control(character) or character.isspace()


def _read_authority(value: str, index: int, limit: int) -> tuple[list[str], int] | None:
    """(authority characters, index after them) up to the first separator."""
    authority: list[str] = []
    while index < limit:
        step = _authority_step(value, index, limit)
        if step is None:
            return None
        character, index = step
        if character is None:
            return authority, index
        authority.append(character)
    return authority, index


def _authority_step(value: str, index: int, limit: int) -> tuple[str | None, int] | None:
    """(character, index after it); the character is None at a separator; None if refused."""
    decoded = _uri_character(value, index, limit)
    if decoded is None:
        return None
    return _authority_character_step(decoded[0], decoded[1])


def _authority_character_step(character: str, index: int) -> tuple[str | None, int] | None:
    if character in {"/", "\\"}:
        return None, index
    if _authority_character_is_invalid(character):
        return None
    return character, index


def _uri_authority_end(value: str, index: int, limit: int) -> int | None:
    """The index after a `localhost` authority, or None when it is anything else."""
    read = _read_authority(value, index, limit)
    if read is None:
        return None
    authority, index = read
    if "".join(authority).casefold() != "localhost":
        return None
    return index


def _native_windows_prefix(
    value: str, start: int, limit: int
) -> tuple[str, int] | None:
    drive = value[start : start + 2].casefold()
    index = start + 2
    if value[index : index + 1] not in {"/", "\\"}:
        return None
    while value[index : index + 1] in {"/", "\\"} and index < limit:
        index += 1
    return drive, index


def _windows_uri_drive_start(
    value: str, start: int, limit: int
) -> tuple | None:
    """The drive pair for a `file:` URI, after any authority it carries."""
    index, leading_separators = _skip_uri_separators(value, start + 5, limit)
    pair = _uri_drive_pair(value, index, limit)
    if _names_windows_drive(pair):
        return pair
    if not leading_separators:
        return None
    return _uri_drive_pair_after_authority(value, index, limit)


def _uri_drive_pair_after_authority(value: str, index: int, limit: int) -> tuple | None:
    authority_end = _uri_authority_end(value, index, limit)
    if authority_end is None:
        return None
    index, _separators = _skip_uri_separators(value, authority_end, limit)
    return _uri_drive_pair(value, index, limit)


def _windows_semantic_prefix(
    value: str,
    start: int,
    limit: int,
    *,
    file_uri: bool,
) -> tuple[str, int] | None:
    if not file_uri:
        return _native_windows_prefix(value, start, limit)
    return _uri_windows_prefix(value, start, limit)


def _uri_windows_prefix(value: str, start: int, limit: int) -> tuple[str, int] | None:
    pair = _windows_uri_drive_start(value, start, limit)
    if not _names_windows_drive(pair):
        return None
    first, second = pair
    index, separators = _skip_uri_separators(value, second[1], limit)
    if not separators:
        return None
    return (first[0] + ":").casefold(), index


def _windows_semantic_component_text(raw_component: str) -> str | None:
    """The component to add: "" when nothing is added, None when it is refused."""
    if raw_component == ".":
        return ""
    return _trimmed_windows_component(raw_component.rstrip(" ."))


def _windows_add_semantic_component(
    raw_component: str,
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
    drive: str,
) -> tuple[bool, bool]:
    if not _windows_apply_semantic_component(raw_component, components):
        return False, False
    return True, _windows_components_reach_root(drive, components, root)


def _windows_apply_semantic_component(raw_component: str, components: list[str]) -> bool:
    """Apply one raw component to the stack; False when it is refused."""
    if raw_component == "..":
        del components[-1:]
        return True
    return _windows_push_component(_windows_semantic_component_text(raw_component), components)


def _windows_push_component(component: str | None, components: list[str]) -> bool:
    if component is None:
        return False
    if component:
        components.append(component.casefold())
    return True


def _windows_component_accepts_space(
    component: list[str],
    components: list[str],
    root: tuple[str, tuple[frozenset[str], ...]],
) -> bool:
    aliases = root[1]
    if len(components) >= len(aliases):
        return False
    candidate = ("".join(component) + " ").casefold()
    return any(alias.startswith(candidate) for alias in aliases[len(components)])


def _ends_native_component(character: str) -> bool:
    return _is_control(character) or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS


def _native_component_separator(value: str, index: int, limit: int) -> int | None:
    """The index of the separator that ends the component at `index`, or None."""
    separator = index
    while separator < limit and value[separator] not in {"/", "\\"}:
        if _ends_native_component(value[separator]):
            return None
        separator += 1
    if separator >= limit:
        return None
    return separator


def _windows_native_component_is_canceled(
    value: str,
    index: int,
    limit: int,
) -> bool:
    separator = _native_component_separator(value, index, limit)
    if separator is None:
        return False
    while separator < limit and value[separator] in {"/", "\\"}:
        separator += 1
    return (
        value[separator : separator + 2] == ".."
        and value[separator + 2 : separator + 3] in {"", "/", "\\"}
    )


_KEEP_SCANNING = object()


def _windows_uri_terminator(character: str, encoded: bool, file_uri: bool) -> bool:
    if not file_uri or encoded:
        return False
    return character == "#"


def _windows_quote_terminator(character: str, encoded: bool, quoted: bool) -> bool:
    if not quoted or encoded:
        return False
    return character == '"'


def _windows_terminator(
    character: str,
    *,
    encoded: bool,
    quoted: bool,
    file_uri: bool,
    unquoted_space: bool,
    space_allowed: bool,
) -> bool:
    if _windows_structural_terminator(character):
        return True
    if _windows_uri_terminator(character, encoded, file_uri):
        return True
    return _windows_quote_terminator(character, encoded, quoted) or (
        unquoted_space and not space_allowed
    )


def _windows_structural_terminator(character: str) -> bool:
    return _is_control(character) or character in '<>:"|?*'


class _WindowsRootScanner:
    """Walk one candidate Windows token and report where the vault root ends."""

    def __init__(
        self,
        value: str,
        start: int,
        root: tuple[str, tuple[frozenset[str], ...]],
        *,
        file_uri: bool,
        quoted: bool,
    ) -> None:
        self.value = value
        self.start = start
        self.root = root
        self.file_uri = file_uri
        self.quoted = quoted
        self.limit = min(
            len(value), start + _windows_candidate_inspection_characters(root)
        )
        self.index = start
        self.drive = ""
        self.components: list[str] = []
        self.component: list[str] = []
        self.component_source_end = start
        self.disposable_component: bool | None = None

    def run(self) -> int | None:
        if not self._enter_prefix():
            return None
        if not self.root[1]:
            return self.index
        return self._scan()

    def _enter_prefix(self) -> bool:
        """Consume the drive prefix; False when it is not the root's drive."""
        prefix = _windows_semantic_prefix(
            self.value, self.start, self.limit, file_uri=self.file_uri
        )
        if prefix is None:
            return False
        self.drive, self.index = prefix
        if self.drive != self.root[0]:
            return False
        self.component_source_end = self.index
        return True

    def _scan(self) -> int | None:
        while self.index < self.limit:
            outcome = self._step()
            if outcome is not _KEEP_SCANNING:
                return outcome
        return self._finish()

    def _decode(self) -> tuple | None:
        if not self.file_uri:
            return self.value[self.index], self.index + 1, False
        return _uri_character(self.value, self.index, self.limit)

    def _space_allowed(self, character: str, unquoted_space: bool) -> bool:
        """A native log line may hold a space the component itself allows."""
        if not unquoted_space or self.file_uri or character != " ":
            return False
        if _windows_component_accepts_space(
            self.component, self.components, self.root
        ):
            return True
        return self._component_is_disposable()

    def _component_is_disposable(self) -> bool:
        if self.disposable_component is None:
            self.disposable_component = _windows_native_component_is_canceled(
                self.value, self.index, self.limit
            )
        return self.disposable_component

    def _close_component(self, source_end: int, terminator: bool) -> object:
        valid, matched = _windows_add_semantic_component(
            "".join(self.component), self.components, self.root, self.drive
        )
        if not valid:
            return None
        if matched:
            return self.component_source_end
        return self._open_next_component(source_end, terminator)

    def _open_next_component(self, source_end: int, terminator: bool) -> object:
        self.component.clear()
        self.disposable_component = None
        if terminator:
            return None
        self.index = source_end
        self.component_source_end = source_end
        return _KEEP_SCANNING

    def _terminator_for(self, character: str, encoded: bool) -> bool:
        unquoted_space = not self.quoted and not encoded and character.isspace()
        space_allowed = self._space_allowed(character, unquoted_space)
        return _windows_terminator(
            character,
            encoded=encoded,
            quoted=self.quoted,
            file_uri=self.file_uri,
            unquoted_space=unquoted_space,
            space_allowed=space_allowed,
        )

    def _step(self) -> object:
        decoded = self._decode()
        if decoded is None:
            return None
        character, source_end, encoded = decoded
        terminator = self._terminator_for(character, encoded)
        if character in {"/", "\\"} or terminator:
            return self._close_component(source_end, terminator)
        self.component.append(character)
        self.component_source_end = source_end
        self.index = source_end
        return _KEEP_SCANNING

    def _finish(self) -> int | None:
        valid, matched = _windows_add_semantic_component(
            "".join(self.component), self.components, self.root, self.drive
        )
        if valid and matched:
            return self.component_source_end
        return None


def _windows_semantic_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[frozenset[str], ...]],
    *,
    file_uri: bool,
    quoted: bool,
) -> int | None:
    return _WindowsRootScanner(
        value, start, root, file_uri=file_uri, quoted=quoted
    ).run()


def _windows_end_always(character: str) -> bool:
    return _is_control(character) or character in _WINDOWS_TOKEN_STRUCTURAL_TERMINATORS


def _windows_end_in_context(character: str, *, file_uri: bool, quoted: bool) -> bool:
    if file_uri and character == "#":
        return True
    if quoted:
        return character == '"'
    return character.isspace() or character in ":,;)]}"


def _windows_end_character(character: str, *, file_uri: bool, quoted: bool) -> bool:
    if _windows_end_always(character):
        return True
    return _windows_end_in_context(character, file_uri=file_uri, quoted=quoted)


def _percent_escape_end(value: str, index: int, limit: int) -> int | None:
    if index + 3 > limit:
        return None
    if re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1 : index + 3]) is None:
        return None
    return index + 3


def _windows_next_index(
    value: str, index: int, limit: int, *, file_uri: bool, quoted: bool
) -> int | None:
    """Where the token continues after `index`; None where it ends."""
    character = value[index]
    if _windows_end_character(character, file_uri=file_uri, quoted=quoted):
        return None
    if file_uri and character == "%":
        return _percent_escape_end(value, index, limit)
    return index + 1


def _windows_token_scan_end(
    value: str, root_end: int, limit: int, *, file_uri: bool, quoted: bool
) -> int:
    index = root_end
    while index < limit:
        next_index = _windows_next_index(value, index, limit, file_uri=file_uri, quoted=quoted)
        if next_index is None:
            return index
        index = next_index
    return index


def _strip_log_punctuation(value: str, root_end: int, match_end: int) -> int:
    """Trailing log punctuation is not path unless it directly follows a separator."""
    punctuation_start = match_end
    while punctuation_start > root_end and (
        value[punctuation_start - 1] in _WINDOWS_LOG_TRAILING_PUNCTUATION
    ):
        punctuation_start -= 1
    if punctuation_start == root_end:
        return punctuation_start
    if value[punctuation_start - 1 : punctuation_start] not in {"/", "\\"}:
        return punctuation_start
    return match_end


def _windows_redaction_end(
    value: str,
    start: int,
    root_end: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> int:
    limit = min(len(value), start + _MAX_REDACTION_PATH_TOKEN)
    match_end = _windows_token_scan_end(value, root_end, limit, file_uri=file_uri, quoted=quoted)
    return _strip_log_punctuation(value, root_end, match_end)


def _token_bounded(previous: str) -> bool:
    """A token starts only where the previous character could not continue one."""
    if not previous:
        return True
    return not (previous.isalnum() or previous in "_\\/%")


def _windows_native_start(value: str, start: int, bounded: bool) -> int | None:
    if not bounded:
        return None
    if _drive_spec_at(value, start):
        return start
    return _windows_extended_local_start(value, start)


def _windows_token_at(value: str, start: int) -> tuple[bool, int | None] | None:
    """(file_uri, native start) of a candidate at `start`; None when none starts here."""
    bounded = _token_bounded(value[start - 1 : start])
    file_uri = bounded and value[start : start + 5].casefold() == "file:"
    native_start = _windows_native_start(value, start, bounded)
    if not file_uri and native_start is None:
        return None
    return file_uri, native_start


def _windows_root_end(
    value: str,
    start: int,
    root: tuple[str, tuple[frozenset[str], ...]],
    *,
    file_uri: bool,
    native_start: int | None,
    quoted: bool,
) -> int | None:
    if native_start is not None:
        root_end = _windows_native_root_match_end(value, native_start, root, quoted=quoted)
        if root_end is not None:
            return root_end
    scan_start = start if native_start is None else native_start
    return _windows_semantic_root_match_end(
        value, scan_start, root, file_uri=file_uri, quoted=quoted
    )


def _windows_match_end_at(
    value: str, start: int, root: tuple[str, tuple[frozenset[str], ...]]
) -> int | None:
    token = _windows_token_at(value, start)
    if token is None:
        return None
    file_uri, native_start = token
    quoted = value[start - 1 : start] == '"'
    root_end = _windows_root_end(
        value, start, root, file_uri=file_uri, native_start=native_start, quoted=quoted
    )
    if root_end is None:
        return None
    return _windows_redaction_end(value, start, root_end, file_uri=file_uri, quoted=quoted)


def _redact_windows_path_tokens(value: str, path: Path, marker: str) -> str:
    root = _windows_root_component_aliases(path)
    if root is None:
        return value
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        start = index
        match_end = _windows_match_end_at(value, start, root)
        if match_end is None:
            index = start + 1
            continue
        pieces.append(value[cursor:start])
        pieces.append(marker)
        cursor = match_end
        index = max(start + 1, match_end)
    pieces.append(value[cursor:])
    return "".join(pieces)


def _posix_text_within_ceiling(raw: str, encoded: bytes) -> bool:
    return len(raw) <= _MAX_REDACTION_PATH_TOKEN and len(encoded) <= _MAX_REDACTION_PATH_TOKEN


def _posix_text_is_rooted(raw: str, encoded: bytes) -> bool:
    if not raw.startswith("/") or raw.startswith("//"):
        return False
    if not _posix_text_within_ceiling(raw, encoded):
        return False
    return not any(_is_control(character) for character in raw)


def _posix_component_within_ceiling(component: str, component_bytes: bytes) -> bool:
    if len(component) > _MAX_COMPONENT_CHARACTERS:
        return False
    return len(component_bytes) <= _MAX_COMPONENT_BYTES


def _posix_component_is_valid(component: str) -> bool:
    try:
        component_bytes = component.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if not _posix_component_within_ceiling(component, component_bytes):
        return False
    return not any(_is_control(character) or character == "/" for character in component)


def _all_posix_components_valid(components: tuple[str, ...]) -> bool:
    return all(_posix_component_is_valid(component) for component in components)


def _split_posix_components(normalized: str) -> tuple[str, ...]:
    return tuple(component for component in normalized.split("/") if component)


def _normalized_posix_root(raw: str) -> tuple[str, tuple[str, ...]] | None:
    normalized = posixpath.normpath(raw)
    if not normalized.startswith("/") or normalized.startswith("//"):
        return None
    components = _split_posix_components(normalized)
    if len(components) > _MAX_COMPONENTS:
        return None
    return normalized, components


def _posix_root_components(
    path: Path,
) -> tuple[str, tuple[str, ...]] | None:
    try:
        raw = path.as_posix()
        encoded = raw.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeError):
        return None
    if not _posix_text_is_rooted(raw, encoded):
        return None
    root = _normalized_posix_root(raw)
    if root is None or not _all_posix_components_valid(root[1]):
        return None
    return root


def _posix_candidate_inspection_characters(
    root: tuple[str, tuple[str, ...]],
) -> int:
    root_characters = len(root[0]) + len(root[1])
    return min(
        _MAX_REDACTION_PATH_TOKEN,
        _MAX_REDACTION_PATH_TOKEN // 2 + root_characters * 12,
    )


def _posix_candidate_starts_at(value: str, start: int) -> bool:
    return value[start : start + 5].casefold() == "file:" or value[
        start : start + 1
    ] == "/"


def _boundary_character_at(value: str, index: int, file_uri: bool) -> tuple[str, bool] | None:
    """(character, encoded) at `index`; None when a file URI escape there is malformed."""
    if not file_uri:
        return value[index], False
    decoded = _uri_character(value, index, min(len(value), index + 12))
    if decoded is None:
        return None
    return decoded[0], decoded[2]


def _posix_hard_boundary(character: str, quoted: bool) -> bool:
    if character in ':?#<>"' or character.isspace() or _is_control(character):
        return True
    return quoted and character == '"'


def _posix_punctuation_boundary(value: str, index: int) -> bool:
    character = value[index]
    if character not in _WINDOWS_LOG_TRAILING_PUNCTUATION:
        return False
    punctuation_end = _trailing_punctuation_end(value, index)
    if _after_punctuation_is_boundary(value, punctuation_end):
        return True
    return character in {",", ";"} and _posix_candidate_starts_at(value, punctuation_end)


def _decoded_posix_boundary(
    character: str, encoded: bool, quoted: bool, value: str, index: int
) -> bool:
    if character == "/":
        return True
    if encoded:
        return False
    return _posix_hard_boundary(character, quoted) or _posix_punctuation_boundary(value, index)


def _posix_root_boundary(
    value: str,
    index: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> bool:
    if index >= len(value):
        return True
    decoded = _boundary_character_at(value, index, file_uri)
    if decoded is None:
        return False
    return _decoded_posix_boundary(decoded[0], decoded[1], quoted, value, index)


def _posix_native_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[str, ...]],
    *,
    quoted: bool,
) -> int | None:
    root_text = root[0]
    if not value.startswith(root_text, start):
        return None
    end = start + len(root_text)
    if _posix_root_boundary(value, end, file_uri=False, quoted=quoted):
        return end
    return None


def _native_posix_prefix(value: str, start: int, limit: int) -> int | None:
    if value[start : start + 1] != "/":
        return None
    index = start
    while index < limit and value[index : index + 1] == "/":
        index += 1
    return index


def _unsafe_authority_character(character: str, encoded: bool) -> bool:
    if _is_control(character) or character.isspace() or character == "\\":
        return True
    return not encoded and character in "?#"


def _collected_authority(
    value: str, index: int, limit: int
) -> tuple[str, int] | None:
    authority: list[str] = []
    while index < limit:
        step = _collected_authority_step(value, index, limit)
        if step is None:
            return None
        character, index = step
        if character is None:
            return "".join(authority), index
        authority.append(character)
    return None


def _collected_authority_step(value: str, index: int, limit: int) -> tuple[str | None, int] | None:
    """(character, index after it); the character is None at "/"; None if refused."""
    decoded = _uri_character(value, index, limit)
    if decoded is None:
        return None
    return _collected_authority_character(*decoded)


def _collected_authority_character(
    character: str, source_end: int, encoded: bool
) -> tuple[str | None, int] | None:
    if character == "/":
        return None, source_end
    if _unsafe_authority_character(character, encoded):
        return None
    return character, source_end


def _posix_authority_end(value: str, index: int, limit: int) -> int | None:
    """The index after a `localhost` authority, or None for anything else."""
    collected = _collected_authority(value, index, limit)
    if collected is None:
        return None
    authority, end = collected
    if authority.casefold() != "localhost":
        return None
    return end


def _skip_posix_separators(value: str, index: int, limit: int) -> int:
    while index < limit:
        decoded = _uri_character(value, index, limit)
        if decoded is None or decoded[0] != "/":
            return index
        index = decoded[1]
    return index


_NOT_ROOTED = object()


def _rooted_uri_prefix(value: str, index: int, limit: int) -> object:
    """The path start after `file:///`, None when a fourth slash makes it
    unsafe, or `_NOT_ROOTED` when an authority follows instead."""
    third = _uri_character(value, index, limit)
    if third is None or third[0] != "/":
        return _NOT_ROOTED
    index = third[1]
    fourth = _uri_character(value, index, limit)
    if fourth is not None and fourth[0] == "/":
        return None
    return index


def _double_slash_start(value: str, start: int, limit: int) -> tuple[int, bool]:
    """(index after `file://`, whether both slashes were there)."""
    index = start + 5
    first = _uri_character(value, index, limit)
    if first is None or first[0] != "/":
        return index, False
    index = first[1]
    second = _uri_character(value, index, limit)
    if second is None or second[0] != "/":
        return index, False
    return second[1], True


def _uri_posix_after_double_slash(value: str, index: int, limit: int) -> int | None:
    rooted = _rooted_uri_prefix(value, index, limit)
    if rooted is not _NOT_ROOTED:
        return rooted
    authority_end = _posix_authority_end(value, index, limit)
    if authority_end is None:
        return None
    return _skip_posix_separators(value, authority_end, limit)


def _uri_posix_prefix(value: str, start: int, limit: int) -> int | None:
    first = _uri_character(value, start + 5, limit)
    if first is None or first[0] != "/":
        return None
    index, doubled = _double_slash_start(value, start, limit)
    if not doubled:
        return index
    return _uri_posix_after_double_slash(value, index, limit)


def _posix_semantic_prefix(
    value: str,
    start: int,
    limit: int,
    *,
    file_uri: bool,
) -> int | None:
    if not file_uri:
        return _native_posix_prefix(value, start, limit)
    return _uri_posix_prefix(value, start, limit)


def _posix_components_reach_root(
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> bool:
    root_components = root[1]
    return len(components) == len(root_components) and all(
        component == expected
        for component, expected in zip(components, root_components)
    )


def _posix_components_end_at_root(
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> bool:
    root_components = root[1]
    return (
        bool(root_components)
        and len(components) >= len(root_components)
        and all(
            component == expected
            for component, expected in zip(
                components[-len(root_components) :], root_components
            )
        )
    )


def _posix_add_semantic_component(
    raw_component: str,
    components: list[str],
    root: tuple[str, tuple[str, ...]],
) -> tuple[bool, bool]:
    if not _posix_apply_semantic_component(raw_component, components):
        return False, False
    return True, _posix_components_reach_root(components, root)


def _posix_apply_semantic_component(raw_component: str, components: list[str]) -> bool:
    """Apply one raw component to the stack; False when it is refused."""
    if raw_component == "..":
        del components[-1:]
        return True
    return _posix_push_component(raw_component, components)


def _posix_push_component(raw_component: str, components: list[str]) -> bool:
    if raw_component in {"", "."}:
        return True
    if not _posix_component_is_valid(raw_component):
        return False
    components.append(raw_component)
    return True


def _posix_component_accepts_log_character(
    component: list[str],
    components: list[str],
    root: tuple[str, tuple[str, ...]],
    character: str,
) -> bool:
    if len(components) >= len(root[1]):
        return False
    return root[1][len(components)].startswith("".join(component) + character)


def _ends_posix_component(character: str) -> bool:
    return _is_control(character) or character in '<>"'


def _posix_component_separator(value: str, index: int, limit: int) -> int | None:
    """The index of the `/` that ends the component at `index`, or None."""
    separator = index
    while separator < limit and value[separator] != "/":
        if _ends_posix_component(value[separator]):
            return None
        separator += 1
    if separator >= limit:
        return None
    return separator


def _posix_native_component_is_canceled(
    value: str,
    index: int,
    limit: int,
) -> bool:
    separator = _posix_component_separator(value, index, limit)
    if separator is None:
        return False
    while separator < limit and value[separator] == "/":
        separator += 1
    return (
        value[separator : separator + 2] == ".."
        and value[separator + 2 : separator + 3] in {"", "/"}
    )


def _posix_unquoted_boundary(character: str, encoded: bool, quoted: bool) -> bool:
    if quoted or encoded:
        return False
    return character.isspace() or character in ":,;)]}<>\""


def _posix_uri_terminator(character: str, encoded: bool, file_uri: bool) -> bool:
    if not file_uri or encoded:
        return False
    return character in "?#\\"


def _posix_quote_terminator(character: str, encoded: bool, quoted: bool) -> bool:
    if not quoted or encoded:
        return False
    return character == '"'


def _posix_terminator(
    character: str,
    *,
    encoded: bool,
    quoted: bool,
    file_uri: bool,
    unquoted_boundary: bool,
    boundary_allowed: bool,
) -> bool:
    if _is_control(character):
        return True
    if _posix_uri_terminator(character, encoded, file_uri):
        return True
    return _posix_quote_terminator(character, encoded, quoted) or (
        unquoted_boundary and not boundary_allowed
    )


class _PosixRootScanner:
    """Walk one candidate path token and report where the vault root ends."""

    def __init__(
        self,
        value: str,
        start: int,
        root: tuple[str, tuple[str, ...]],
        *,
        file_uri: bool,
        quoted: bool,
    ) -> None:
        self.value = value
        self.start = start
        self.root = root
        self.file_uri = file_uri
        self.quoted = quoted
        self.limit = min(
            len(value), start + _posix_candidate_inspection_characters(root)
        )
        self.index = start
        self.components: list[str] = []
        self.component: list[str] = []
        self.component_source_end = start
        self.disposable_component: bool | None = None

    def run(self) -> tuple[int | None, int]:
        index = _posix_semantic_prefix(
            self.value, self.start, self.limit, file_uri=self.file_uri
        )
        if index is None:
            return None, self.start + 1
        self.index = index
        self.component_source_end = index
        if not self.root[1]:
            return index, index
        return self._scan()

    def _scan(self) -> tuple[int | None, int]:
        while self.index < self.limit:
            outcome = self._step()
            if outcome is not None:
                return outcome
        return self._finish()

    def _decode(self) -> tuple | None:
        if not self.file_uri:
            return self.value[self.index], self.index + 1, False
        return _uri_character(self.value, self.index, self.limit)

    def _boundary_allowed(self, character: str, unquoted_boundary: bool) -> bool:
        """A native log line may hold a character the path itself allows."""
        if not unquoted_boundary or self.file_uri:
            return False
        if _posix_component_accepts_log_character(
            self.component, self.components, self.root, character
        ):
            return True
        return self._component_is_disposable()

    def _component_is_disposable(self) -> bool:
        if self.disposable_component is None:
            self.disposable_component = _posix_native_component_is_canceled(
                self.value, self.index, self.limit
            )
        return self.disposable_component

    def _root_result(self, source_end: int) -> tuple:
        if _posix_root_boundary(
            self.value,
            self.component_source_end,
            file_uri=self.file_uri,
            quoted=self.quoted,
        ):
            return self.component_source_end, self.component_source_end
        return None, source_end

    def _close_component(self, source_end: int, terminator: bool) -> tuple | None:
        valid, matched = _posix_add_semantic_component(
            "".join(self.component), self.components, self.root
        )
        if not valid:
            return None, self.index
        if matched or _posix_components_end_at_root(self.components, self.root):
            return self._root_result(source_end)
        return self._open_next_component(source_end, terminator)

    def _open_next_component(self, source_end: int, terminator: bool) -> tuple | None:
        self.component.clear()
        self.disposable_component = None
        if terminator:
            return None, self.index
        self.index = source_end
        self.component_source_end = source_end
        return None

    def _terminator_for(self, character: str, encoded: bool) -> bool:
        unquoted_boundary = _posix_unquoted_boundary(character, encoded, self.quoted)
        boundary_allowed = self._boundary_allowed(character, unquoted_boundary)
        return _posix_terminator(
            character,
            encoded=encoded,
            quoted=self.quoted,
            file_uri=self.file_uri,
            unquoted_boundary=unquoted_boundary,
            boundary_allowed=boundary_allowed,
        )

    def _step(self) -> tuple | None:
        decoded = self._decode()
        if decoded is None:
            return None, max(self.start + 1, self.index + 1)
        character, source_end, encoded = decoded
        terminator = self._terminator_for(character, encoded)
        if character == "/" or terminator:
            return self._close_component(source_end, terminator)
        self.component.append(character)
        self.component_source_end = source_end
        self.index = source_end
        return None

    def _finish(self) -> tuple[int | None, int]:
        valid, matched = _posix_add_semantic_component(
            "".join(self.component), self.components, self.root
        )
        if valid and (
            matched or _posix_components_end_at_root(self.components, self.root)
        ):
            return self.component_source_end, self.component_source_end
        return None, self.limit


def _posix_semantic_root_match_end(
    value: str,
    start: int,
    root: tuple[str, tuple[str, ...]],
    *,
    file_uri: bool,
    quoted: bool,
) -> tuple[int | None, int]:
    return _PosixRootScanner(
        value, start, root, file_uri=file_uri, quoted=quoted
    ).run()


def _posix_end_always(character: str, file_uri: bool) -> bool:
    if _is_control(character) or character in '<>"':
        return True
    return file_uri and character in "?#\\"


def _posix_end_in_context(character: str, quoted: bool) -> bool:
    if quoted:
        return character == '"'
    return character.isspace() or character in ":,;)]}"


def _posix_end_character(character: str, *, file_uri: bool, quoted: bool) -> bool:
    if _posix_end_always(character, file_uri):
        return True
    return _posix_end_in_context(character, quoted)


def _posix_next_index(
    value: str, index: int, limit: int, *, file_uri: bool, quoted: bool
) -> int | None:
    """Where the token continues after `index`; None where it ends."""
    character = value[index]
    if _posix_end_character(character, file_uri=file_uri, quoted=quoted):
        return None
    if file_uri and character == "%":
        return _percent_escape_end(value, index, limit)
    return index + 1


def _posix_redaction_end(
    value: str,
    start: int,
    root_end: int,
    *,
    file_uri: bool,
    quoted: bool,
) -> int:
    limit = min(len(value), start + _MAX_REDACTION_PATH_TOKEN)
    index = root_end
    while index < limit:
        next_index = _posix_next_index(value, index, limit, file_uri=file_uri, quoted=quoted)
        if next_index is None:
            return index
        index = next_index
    return index


def _posix_token_bounded(value: str, start: int) -> bool:
    """A token starts only where the previous character could not continue one."""
    previous = value[start - 1 : start]
    if previous and (previous.isalnum() or previous in "_/%"):
        return False
    return not (previous == ":" and value[start + 1 : start + 2] == "/")


def _posix_token_at(value: str, start: int) -> tuple[bool, bool] | None:
    """(file_uri, native) of a candidate at `start`; None when none starts here."""
    bounded = _posix_token_bounded(value, start)
    file_uri = bounded and value[start : start + 5].casefold() == "file:"
    native = bounded and value[start : start + 1] == "/"
    if not file_uri and not native:
        return None
    return file_uri, native


def _posix_root_end(
    value: str,
    start: int,
    root: tuple[str, tuple[str, ...]],
    *,
    file_uri: bool,
    native: bool,
    quoted: bool,
) -> tuple[int | None, int]:
    """(root end, where to resume when there is none)."""
    if native:
        root_end = _posix_native_root_match_end(value, start, root, quoted=quoted)
        if root_end is not None:
            return root_end, start + 1
    return _posix_semantic_root_match_end(value, start, root, file_uri=file_uri, quoted=quoted)


def _posix_match_at(
    value: str, start: int, root: tuple[str, tuple[str, ...]]
) -> tuple[int | None, int]:
    """(match end, resume index): the span to redact at `start`, or where to look next."""
    token = _posix_token_at(value, start)
    if token is None:
        return None, start + 1
    file_uri, native = token
    quoted = value[start - 1 : start] == '"'
    root_end, resume = _posix_root_end(
        value, start, root, file_uri=file_uri, native=native, quoted=quoted
    )
    if root_end is None:
        return None, resume
    return _posix_redaction_end(value, start, root_end, file_uri=file_uri, quoted=quoted), resume


def _redact_posix_path_tokens(value: str, path: Path, marker: str) -> str:
    root = _posix_root_components(path)
    if root is None:
        return value
    pieces: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        start = index
        match_end, resume = _posix_match_at(value, start, root)
        if match_end is None:
            index = max(start + 1, resume)
            continue
        pieces.append(value[cursor:start])
        pieces.append(marker)
        cursor = match_end
        index = max(start + 1, match_end)
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_path(value: str, path: Path, marker: str) -> str:
    windows_path = bool(PureWindowsPath(str(path)).drive)
    if windows_path:
        return _redact_windows_path_tokens(value, path, marker)
    return _redact_posix_path_tokens(value, path, marker)


def _collapses_to_space(character: str) -> bool:
    if ord(character) < 32 or 127 <= ord(character) <= 159:
        return True
    return unicodedata.category(character) in {"Zl", "Zp"}


class _LogScanner:
    """Drop terminal control sequences while keeping every printable character."""

    _STRING_INTRODUCERS = {
        "P": False,
        "X": False,
        "]": True,
        "^": False,
        "_": False,
    }
    _C1_STRING_INTRODUCERS = {
        "\x90": False,
        "\x98": False,
        "\x9d": True,
        "\x9e": False,
        "\x9f": False,
    }
    _C1_INTRODUCERS = frozenset(("\x9b", *_C1_STRING_INTRODUCERS))

    def __init__(self, value: str) -> None:
        self.value = value
        self.pieces: list[str] = []
        self.index = 0
        self.state = "ground"
        self.osc = False

    def run(self) -> str:
        while self.index < len(self.value):
            self._step()
        return "".join(self.pieces)

    def _step(self) -> None:
        character = self.value[self.index]
        if self._consumed_by_sequence(character):
            return
        self._emit(character)
        self.index += 1

    def _consumed_by_sequence(self, character: str) -> bool:
        """The first handler, in order, that takes the character; the rest never run."""
        handlers = (
            self._string_terminated,
            self._interrupted_by_introducer,
            self._inside_sequence,
            self._introduces_sequence,
        )
        return any(handler(character) for handler in handlers)

    def _escape_terminator_follows(self, character: str) -> bool:
        return character == "\x1b" and self.value[self.index + 1 : self.index + 2] == "\\"

    def _string_terminator_length(self, character: str) -> int:
        if character == "\x9c":
            return 1
        if self._escape_terminator_follows(character):
            return 2
        return self._bell_terminator_length(character)

    def _bell_terminator_length(self, character: str) -> int:
        if self.osc and character == "\x07":
            return 1
        return 0

    def _string_terminated(self, character: str) -> bool:
        if self.state != "string":
            return False
        length = self._string_terminator_length(character)
        if not length:
            return False
        self.state = "ground"
        self.index += length
        return True

    def _interrupted_by_introducer(self, character: str) -> bool:
        """A new introducer inside a sequence abandons the unfinished one."""
        if self.state == "ground":
            return False
        if character != "\x1b" and character not in self._C1_INTRODUCERS:
            return False
        self.state = "ground"
        return True

    @staticmethod
    def _escape_next_state(character: str) -> str:
        if "0" <= character <= "~":
            return "ground"
        if not " " <= character <= "/":
            return "ground"
        return "escape"

    def _inside_sequence(self, character: str) -> bool:
        if self.state == "ground":
            return False
        self.index += 1
        self.state = self._state_after(character)
        return True

    def _state_after(self, character: str) -> str:
        """The state after one character inside an escape, CSI or string sequence."""
        if self.state == "escape":
            return self._escape_next_state(character)
        if self.state == "csi" and "@" <= character <= "~":
            return "ground"
        return self.state

    def _skip_known_escape(self, following: str) -> None:
        if " " <= following <= "/":
            self.state = "escape"
            self.index += 2
            return
        if "0" <= following <= "~":
            self.index += 2
            return
        self.index += 1

    def _skip_escape_body(self, following: str) -> None:
        if following == "\\":
            self.index += 2
            return
        if not following:
            self.index += 1
            return
        self._skip_known_escape(following)

    def _escape_introducer(self) -> None:
        following = self.value[self.index + 1 : self.index + 2]
        if following == "[":
            self.state = "csi"
            self.index += 2
            return
        if following in self._STRING_INTRODUCERS:
            self.state = "string"
            self.osc = self._STRING_INTRODUCERS[following]
            self.index += 2
            return
        self._skip_escape_body(following)

    def _introduces_sequence(self, character: str) -> bool:
        if character == "\x1b":
            self._escape_introducer()
            return True
        return self._introduces_c1_sequence(character)

    def _introduces_c1_sequence(self, character: str) -> bool:
        if character == "\x9b":
            self.state = "csi"
            self.index += 1
            return True
        if character in self._C1_STRING_INTRODUCERS:
            self.state = "string"
            self.osc = self._C1_STRING_INTRODUCERS[character]
            self.index += 1
            return True
        return False

    def _append_safe_space(self) -> None:
        if not self.pieces or self.pieces[-1] != " ":
            self.pieces.append(" ")

    def _emit(self, character: str) -> None:
        if not _is_control(character):
            self.pieces.append(character)
            return
        if _collapses_to_space(character):
            self._append_safe_space()


def _normalize_log_text(value: str) -> str:
    return _LogScanner(value).run()


def _require_redaction_input(value: object, repository: RepositoryScope | None):
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if repository is None:
        return None
    return _require_repository(repository)


def _oversized_for_redaction(value: str) -> bool:
    if len(value) > _MAX_REDACTION_RAW_BYTES:
        return True
    return not _fits_utf8_redaction_ceiling(value)


def _home_paths() -> list[Path]:
    """The home directory as given and, when it differs, as resolved."""
    home = Path.home().absolute()
    home_paths = [home]
    try:
        resolved_home = home.resolve(strict=False)
    except (OSError, RuntimeError):
        return home_paths
    if resolved_home not in home_paths:
        home_paths.append(resolved_home)
    return home_paths


def _redact_home(redacted: str) -> str:
    try:
        for home_path in _home_paths():
            redacted = _redact_path(redacted, home_path, "<home>")
    except (OSError, RuntimeError):
        pass
    return redacted


def redact_lsp_text(
    value: str,
    *,
    repository: RepositoryScope | None = None,
) -> str:
    """Remove credentials, local roots, and log injection from bounded raw text."""
    repository = _require_redaction_input(value, repository)
    if _oversized_for_redaction(value):
        return _OVERSIZED_REDACTION_MARKER
    redacted = _redact_url_userinfo(_redact_assignments(_normalize_log_text(value)))
    if repository is not None:
        redacted = _redact_path(redacted, Path(repository.checkout_root), "<repository>")
    return _normalize_log_text(_redact_home(redacted))[:1024]


__all__ = [
    "PathContainmentError",
    "RepositorySource",
    "normalize_provider_uri",
    "redact_lsp_text",
    "resolve_repository_source",
    "validate_repository_relative_path",
]
