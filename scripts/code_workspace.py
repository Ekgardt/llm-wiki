"""Bounded repository capture and sealed analyzer workspaces."""
from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import windows_workspace as _windows_workspace
from code_languages import language_for_path
from corpus_snapshot import (
    CapturedSource,
    CodeCaptureContract,
    CodeCaptureFile,
    CorpusSnapshot,
    DirectoryMembership,
    FileStatMetadata,
    RepositoryCodeLimits,
    RepositoryCodePolicy,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
    canonical_retrieval_chunks,
    canonical_source_manifest_sha256,
)
from reliable_memory import canonical_json_bytes
from repository_scope import resolve_repository_scope

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_FILE_ID_RE = re.compile(r"[0-9a-f]{32}")
_ALWAYS_IGNORED = frozenset(
    {".git", ".venv", "venv", "env", "cache", "logs", "run", "__pycache__"}
)
_MAX_POLICY_TEXT = 4096


class WorkspaceChanged(RuntimeError):
    """A sealed analyzer workspace no longer matches captured bytes."""


@dataclass(frozen=True, slots=True)
class SealedWorkspace:
    root: Path
    source_manifest_sha256: str
    entries: tuple[tuple[str, int, str], ...]
    owner_only: bool
    read_only_requested: bool
    platform_identities: tuple[tuple[str, int, str, bool], ...] = ()
    directory_flush: bool = False


def _seal_component_barrier(_component: str) -> None:
    return


def _verify_component_barrier(_component: str) -> None:
    return


def workspace_sealing_supported() -> bool:
    """Return whether this platform has a root-relative no-follow boundary."""
    if os.name == "nt":
        return _windows_workspace.capability()[0]
    if os.name == "posix":
        return all(
            (
                hasattr(os, "O_NOFOLLOW"),
                hasattr(os, "O_DIRECTORY"),
                os.open in os.supports_dir_fd,
                os.mkdir in os.supports_dir_fd,
            )
        )
    return False


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_stop(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    _require_stop_arguments(deadline, cancelled)
    if cancelled is not None and cancelled():
        raise TimeoutError("repository code capture cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("repository code capture deadline reached")


def _require_stop_arguments(deadline: object, cancelled: object) -> None:
    if deadline is not None and not _finite_number(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable or None")


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _normalized_root(value: object) -> str:
    raw = _root_text(value)
    normalized = unicodedata.normalize("NFC", raw)
    pure = PurePosixPath(normalized)
    if _unnormalized_root(raw, normalized, pure):
        raise ValueError("repository code root must be a normalized relative POSIX path")
    return pure.as_posix()


def _root_text(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("repository code root must be path-like")
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise TypeError("repository code root must be text")
    return raw


def _unnormalized_root(raw: str, normalized: str, pure: PurePosixPath) -> bool:
    if not raw or len(raw) > _MAX_POLICY_TEXT or normalized == ".":
        return True
    return _rooted_text(normalized, pure) or _traversing_parts(pure)


def _rooted_text(normalized: str, pure: PurePosixPath) -> bool:
    """Backslashes, a POSIX root, or a Windows drive or root anywhere in the text."""
    windows = PureWindowsPath(normalized)
    return "\\" in normalized or pure.is_absolute() or bool(windows.drive) or bool(windows.root)


def _traversing_parts(pure: PurePosixPath) -> bool:
    return any(part in {"", ".", ".."} for part in pure.parts)


def _normalized_values(
    values: Iterable[object],
    *,
    label: str,
    maximum: int,
    transform: Callable[[object], str],
) -> tuple[str, ...]:
    materialized = tuple(values)
    if len(materialized) > maximum:
        raise ValueError(f"{label} has too many entries")
    _require_no_normalization_collision(materialized, label)
    normalized = tuple(sorted(set(transform(value) for value in materialized)))
    return normalized


def _require_no_normalization_collision(materialized: tuple[object, ...], label: str) -> None:
    normalized_inputs: dict[str, str] = {}
    for value in materialized:
        raw = _raw_text(value)
        if raw is not None:
            _claim_normalized_input(normalized_inputs, raw, label)


def _raw_text(value: object) -> str | None:
    raw = os.fspath(value) if isinstance(value, os.PathLike) else value
    if isinstance(raw, str):
        return raw
    return None


def _claim_normalized_input(normalized_inputs: dict[str, str], raw: str, label: str) -> None:
    normalized_input = unicodedata.normalize("NFC", raw)
    previous = normalized_inputs.get(normalized_input)
    if previous is not None and previous != raw:
        raise ValueError(f"{label} contains a Unicode normalization collision")
    normalized_inputs[normalized_input] = raw


def _policy(
    roots: Iterable[str | Path],
    include_globs: Iterable[str],
    ignore_globs: Iterable[str],
    suffixes: Iterable[str],
) -> RepositoryCodePolicy:
    normalized_roots = _normalized_values(
        roots, label="roots", maximum=128, transform=_normalized_root
    )
    _require_distinct_roots(normalized_roots)
    return RepositoryCodePolicy(
        roots=normalized_roots,
        include_globs=_normalized_values(
            include_globs, label="include_globs", maximum=256, transform=_glob_pattern
        ),
        ignore_globs=_normalized_values(
            ignore_globs, label="ignore_globs", maximum=256, transform=_glob_pattern
        ),
        suffixes=_normalized_values(
            suffixes, label="suffixes", maximum=128, transform=_code_suffix
        ),
    )


def _require_distinct_roots(normalized_roots: tuple[str, ...]) -> None:
    if not normalized_roots:
        raise ValueError("at least one repository code root is required")
    if any(_selects_always_ignored(root) for root in normalized_roots):
        raise ValueError("repository code roots must not select always-ignored directories")
    _require_non_overlapping_roots(normalized_roots)


def _selects_always_ignored(root: str) -> bool:
    return any(part.casefold() in _ALWAYS_IGNORED for part in PurePosixPath(root).parts)


def _require_non_overlapping_roots(normalized_roots: tuple[str, ...]) -> None:
    for index, root in enumerate(normalized_roots):
        if any(other.startswith(root + "/") for other in normalized_roots[index + 1 :]):
            raise ValueError("repository code roots must not overlap")


def _bounded_nonempty(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= limit


def _glob_pattern(value: object) -> str:
    if not _bounded_nonempty(value, _MAX_POLICY_TEXT):
        raise ValueError("repository code glob must be a bounded non-empty string")
    normalized = unicodedata.normalize("NFC", value)
    _require_relative_glob(normalized, PurePosixPath(normalized))
    return normalized


def _require_relative_glob(normalized: str, pure: PurePosixPath) -> None:
    if _unnormalized_glob(normalized, pure):
        raise ValueError("repository code glob must be normalized relative POSIX text")
    if any(part == ".." for part in pure.parts):
        raise ValueError("repository code glob must not traverse parents")


def _unnormalized_glob(normalized: str, pure: PurePosixPath) -> bool:
    if _rooted_text(normalized, pure) or normalized.startswith("./"):
        return True
    return "//" in normalized or any(part == "." for part in normalized.split("/"))


def _code_suffix(value: object) -> str:
    if not _bounded_nonempty(value, 128):
        raise ValueError("repository code suffix must be bounded non-empty text")
    normalized = unicodedata.normalize("NFC", value).casefold()
    if not _dotted_suffix(normalized):
        raise ValueError("repository code suffix must begin with a dot")
    return normalized


def _dotted_suffix(normalized: str) -> bool:
    if len(normalized) < 2 or not normalized.startswith("."):
        return False
    return "/" not in normalized and "\\" not in normalized


def _is_unsafe(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _kind(info: os.stat_result) -> str:
    if _is_unsafe(info):
        return "link"
    return _mode_kind(info.st_mode)


def _mode_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _stat_metadata(info: os.stat_result) -> FileStatMetadata:
    return FileStatMetadata(
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        mode=info.st_mode,
        device=info.st_dev,
        inode=info.st_ino,
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    if min(left.st_dev, left.st_ino, right.st_dev, right.st_ino) <= 0:
        return False
    metadata_matches = (
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
    ) == (
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
    )
    change_time_matches = os.name == "nt" or left.st_ctime_ns == right.st_ctime_ns
    return (
        (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
        and metadata_matches
        and change_time_matches
    )


def _open_read(path: Path) -> int:
    # The catalog's Windows implementation uses OPEN_REPARSE_POINT; POSIX uses O_NOFOLLOW.
    from generation_catalog import _open_read_descriptor

    return _open_read_descriptor(path)


def _capture_read_barrier(_descriptor: int) -> None:
    return


def _capture_open_barrier(_path: Path) -> None:
    return


def _capture_entry_identity_barrier(_path: Path) -> None:
    return


def _capture_final_revalidation_barrier(_root: Path) -> None:
    return


def _capture_root_barrier(_root: Path) -> None:
    return


def _descriptor_identity(descriptor: int) -> tuple[object, ...]:
    from generation_catalog import _descriptor_file_identity

    try:
        return _descriptor_file_identity(descriptor)
    except OSError as exc:
        raise RuntimeError("stable descriptor identity is unavailable") from exc


@contextmanager
def _hold_directory_identity(
    path: Path, expected_identity: tuple[object, ...] | None = None
):
    if os.name == "nt":
        with _hold_windows_directory_identity(path, expected_identity):
            yield
        return
    if os.name == "posix":
        with _hold_posix_directory_identity(path, expected_identity):
            yield
        return
    raise RuntimeError("stable directory identity is unavailable")


def _require_expected_directory(
    identity: tuple[object, ...], expected_identity: tuple[object, ...] | None
) -> None:
    if expected_identity is not None and identity != expected_identity:
        raise PermissionError("repository code directory changed before traversal")


def _windows_directory_identity(handle: int) -> tuple[object, ...]:
    from generation_catalog import _windows_handle_file_identity

    try:
        return _windows_handle_file_identity(handle)
    except OSError as exc:
        raise RuntimeError("stable directory identity is unavailable") from exc


def _current_windows_directory_identity(path: Path) -> tuple[object, ...]:
    from generation_catalog import _windows_handle_file_identity
    from markdown_transaction import _close_windows_handle, _open_windows_directory

    try:
        current = _open_windows_directory(path)
        try:
            return _windows_handle_file_identity(current)
        finally:
            _close_windows_handle(current)
    except OSError as exc:
        raise RuntimeError("stable directory identity is unavailable") from exc


@contextmanager
def _hold_windows_directory_identity(
    path: Path, expected_identity: tuple[object, ...] | None
):
    from markdown_transaction import _close_windows_handle, _open_windows_directory

    handle = _open_windows_directory(path)
    try:
        identity = _windows_directory_identity(handle)
        _require_expected_directory(identity, expected_identity)
        yield
        if identity != _current_windows_directory_identity(path):
            raise RuntimeError("repository code directory changed during capture")
    finally:
        _close_windows_handle(handle)


def _held_directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_same_named_directory(
    path: Path, flags: int, identity: tuple[object, ...], error: BaseException
) -> None:
    """Open the name again; it must still be the directory that was held."""
    current = os.open(path, flags)
    try:
        if identity != _descriptor_identity(current):
            raise error
    finally:
        os.close(current)


@contextmanager
def _hold_posix_directory_identity(
    path: Path, expected_identity: tuple[object, ...] | None
):
    flags = _held_directory_flags()
    descriptor = os.open(path, flags)
    try:
        identity = _descriptor_identity(descriptor)
        _require_expected_directory(identity, expected_identity)
        yield
        _require_same_named_directory(
            path, flags, identity, RuntimeError("repository code directory changed during capture")
        )
    finally:
        os.close(descriptor)


@contextmanager
def _hold_capture_root(path: Path):
    absolute = path.absolute()
    if os.name == "posix":
        with _hold_posix_capture_root(absolute) as held:
            yield held
        return
    if os.name == "nt":
        with _hold_windows_capture_root(absolute) as held:
            yield held
        return
    raise RuntimeError("stable checkout root identity is unavailable")


def _lstat_is_unsafe(path: Path) -> bool:
    try:
        return _is_unsafe(path.lstat())
    except OSError:
        return False


def _open_posix_root(absolute: Path, flags: int) -> int:
    try:
        return os.open(absolute, flags)
    except OSError as exc:
        if _lstat_is_unsafe(absolute):
            raise PermissionError("checkout root is a link or reparse point") from exc
        raise


def _canonical_posix_root(absolute: Path, descriptor: int, flags: int) -> tuple[Path, tuple[object, ...]]:
    """(resolved root, held identity), once the name resolves to the held directory."""
    held = os.fstat(descriptor)
    held_identity = _descriptor_identity(descriptor)
    if _is_unsafe(held) or not stat.S_ISDIR(held.st_mode):
        raise PermissionError("checkout root must be a regular non-link directory")
    _capture_root_barrier(absolute)
    resolved = absolute.resolve(strict=True)
    _require_same_named_directory(
        resolved, flags, held_identity, PermissionError("checkout root changed during canonicalization")
    )
    return resolved, held_identity


@contextmanager
def _hold_posix_capture_root(absolute: Path):
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("stable checkout root identity is unavailable")
    flags = _held_directory_flags()
    descriptor = _open_posix_root(absolute, flags)
    try:
        resolved, held_identity = _canonical_posix_root(absolute, descriptor, flags)
        yield resolved, descriptor
        _require_same_named_directory(
            absolute, flags, held_identity, RuntimeError("checkout root changed during capture")
        )
    finally:
        os.close(descriptor)


def _require_same_windows_directory(
    path: Path, identity: tuple[object, ...], error: BaseException
) -> None:
    from generation_catalog import _windows_handle_file_identity
    from markdown_transaction import _close_windows_handle, _open_windows_directory

    named = _open_windows_directory(path)
    try:
        if identity != _windows_handle_file_identity(named):
            raise error
    finally:
        _close_windows_handle(named)


def _canonical_windows_root(absolute: Path, handle: int) -> tuple[Path, tuple[object, ...]]:
    from generation_catalog import _windows_handle_file_identity

    try:
        held_identity = _windows_handle_file_identity(handle)
    except OSError as exc:
        raise PermissionError("stable checkout root identity is unavailable") from exc
    _capture_root_barrier(absolute)
    resolved = absolute.resolve(strict=True)
    current = absolute.lstat()
    canonical = resolved.lstat()
    if _is_unsafe(current) or _is_unsafe(canonical):
        raise PermissionError("checkout root changed during canonicalization")
    _require_same_windows_directory(
        resolved, held_identity, PermissionError("checkout root changed during canonicalization")
    )
    return resolved, held_identity


@contextmanager
def _hold_windows_capture_root(absolute: Path):
    from markdown_transaction import _close_windows_handle, _open_windows_directory

    try:
        handle = _open_windows_directory(absolute)
    except RuntimeError as exc:
        raise PermissionError("checkout root is a link or reparse point") from exc
    try:
        resolved, held_identity = _canonical_windows_root(absolute, handle)
        yield resolved, handle
        _require_same_windows_directory(
            absolute, held_identity, RuntimeError("checkout root changed during capture")
        )
    finally:
        _close_windows_handle(handle)


def _read_candidate(
    path: Path,
    expected: os.stat_result,
    expected_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    if expected.st_size > limits.max_file_bytes:
        raise ValueError("repository code file byte limit exceeded")
    _capture_open_barrier(path)
    descriptor = _open_read(path)
    try:
        return _read_opened_candidate(
            path, descriptor, expected, expected_identity, limits, deadline, cancelled
        )
    finally:
        os.close(descriptor)


def _bounded_content(
    descriptor: int,
    limits: RepositoryCodeLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytearray:
    content = bytearray()
    while True:
        _check_stop(deadline, cancelled)
        chunk = os.read(descriptor, limits.chunk_bytes)
        if not chunk:
            return content
        content.extend(chunk)
        if len(content) > limits.max_file_bytes:
            raise ValueError("repository code file byte limit exceeded")


def _descriptor_unchanged(
    descriptor: int,
    before: os.stat_result,
    before_identity: tuple[object, ...],
    before_state: object,
    content: bytearray,
) -> bool:
    """The descriptor still shows the state, identity, metadata and size read began with."""
    from generation_catalog import _stable_descriptor_state

    after = os.fstat(descriptor)
    return (
        before_state == _stable_descriptor_state(descriptor)
        and before_identity == _descriptor_identity(descriptor)
        and _stat_metadata(before) == _stat_metadata(after)
        and len(content) == before.st_size
    )


def _opened_expected_file(
    before: os.stat_result,
    before_identity: tuple[object, ...],
    expected: os.stat_result,
    expected_identity: tuple[object, ...],
) -> bool:
    if not stat.S_ISREG(before.st_mode) or expected_identity != before_identity:
        return False
    return os.name != "posix" or _same_file(expected, before)


def _read_opened_candidate(
    path: Path,
    descriptor: int,
    expected: os.stat_result,
    expected_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    before = os.fstat(descriptor)
    before_identity = _descriptor_identity(descriptor)
    if not _opened_expected_file(before, before_identity, expected, expected_identity):
        raise PermissionError("repository code file changed before no-follow open")
    from generation_catalog import _stable_descriptor_state

    before_state = _stable_descriptor_state(descriptor)
    content = _bounded_content(descriptor, limits, deadline, cancelled)
    _capture_read_barrier(descriptor)
    if not _descriptor_unchanged(descriptor, before, before_identity, before_state, content):
        raise RuntimeError("repository code file changed during capture")
    _require_named_candidate(path, before, before_identity)
    return bytes(content), _stat_metadata(before)


def _require_named_candidate(
    path: Path, before: os.stat_result, before_identity: tuple[object, ...]
) -> None:
    """The name, opened again without following links, is still the file that was read."""
    current = path.lstat()
    if _is_unsafe(current):
        raise RuntimeError("repository code file changed during capture")
    named_descriptor = _open_read(path)
    try:
        named = os.fstat(named_descriptor)
        if (
            before_identity != _descriptor_identity(named_descriptor)
            or _stat_metadata(before) != _stat_metadata(named)
        ):
            raise RuntimeError("repository code file changed during capture")
    finally:
        os.close(named_descriptor)


def _entry_identity(path: Path, info: os.stat_result) -> tuple[object, ...] | None:
    kind = _kind(info)
    if kind == "file":
        return _file_entry_identity(path, info)
    if kind == "directory":
        return _directory_entry_identity(path, info)
    return None


def _file_entry_identity(path: Path, info: os.stat_result) -> tuple[object, ...]:
    descriptor = _open_read(path)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(info, opened):
            raise PermissionError("repository code file changed during enumeration")
        return _descriptor_identity(descriptor)
    finally:
        os.close(descriptor)


def _directory_entry_identity(path: Path, info: os.stat_result) -> tuple[object, ...]:
    if os.name == "nt":
        return _windows_directory_entry_identity(path, info)
    descriptor = os.open(path, _posix_directory_flags())
    try:
        opened = os.fstat(descriptor)
        if not _same_file(info, opened):
            raise PermissionError("repository code directory changed during enumeration")
        return _descriptor_identity(descriptor)
    finally:
        os.close(descriptor)


def _windows_directory_entry_identity(path: Path, info: os.stat_result) -> tuple[object, ...]:
    from generation_catalog import _windows_handle_file_identity
    from markdown_transaction import _close_windows_handle, _open_windows_directory

    handle = _open_windows_directory(path)
    try:
        _require_windows_directory_unchanged(handle, info)
        return _windows_handle_file_identity(handle)
    finally:
        _close_windows_handle(handle)


def _require_windows_directory_unchanged(handle: int, info: os.stat_result) -> None:
    from generation_catalog import (
        _windows_handle_identity_candidates,
        _windows_stat_matches_any_identity,
    )

    try:
        # Both identities the handle can report: `os.stat` returns
        # the `GetFileInformationByHandle` pair before Python 3.12
        # and the wider `FILE_ID_INFO` pair from 3.12 on, so
        # comparing against only one rejected every unchanged
        # directory on the newer interpreters.
        opened_identities = _windows_handle_identity_candidates(handle)
    except OSError as exc:
        raise RuntimeError(
            "stable Windows directory identity is unavailable"
        ) from exc
    if not _windows_stat_matches_any_identity(info, opened_identities):
        raise PermissionError(
            "repository code directory changed during enumeration"
        )


@contextmanager
def _open_captured_directory(
    root_anchor: int,
    relative_path: str,
    captured_identities: dict[str, tuple[object, ...]],
):
    parts = () if relative_path in {"", "."} else PurePosixPath(relative_path).parts
    opened: list[int] = []
    try:
        yield _descend_captured(root_anchor, parts, captured_identities, opened)
    finally:
        _close_anchors(opened)


def _descend_captured(
    root_anchor: int,
    parts: tuple[str, ...],
    captured_identities: dict[str, tuple[object, ...]],
    opened: list[int],
) -> int:
    """The anchor of the captured directory, each step proven to be the captured one."""
    if not parts:
        _require_root_unchanged(root_anchor, captured_identities.get("."))
        return root_anchor
    current = root_anchor
    prefix: list[str] = []
    for part in parts:
        prefix.append(part)
        current = _open_captured_child(current, part, captured_identities.get("/".join(prefix)), opened)
    return current


def _anchor_identity(anchor: int) -> tuple[object, ...]:
    if os.name == "posix":
        return _descriptor_identity(anchor)
    if os.name == "nt":
        from generation_catalog import _windows_handle_file_identity

        return _windows_handle_file_identity(anchor)
    raise RuntimeError("stable directory traversal is unavailable")


def _require_root_unchanged(anchor: int, expected: tuple[object, ...] | None) -> None:
    if expected is None:
        return
    if _anchor_identity(anchor) != expected:
        raise PermissionError("repository code directory changed before revalidation")


def _open_captured_child(
    current: int, part: str, expected: tuple[object, ...] | None, opened: list[int]
) -> int:
    if expected is None:
        raise RuntimeError("captured directory identity is unavailable")
    child, identity = _open_relative_directory(current, part, opened)
    if identity != expected:
        raise PermissionError("repository code directory changed before revalidation")
    return child


def _open_relative_directory(current: int, part: str, opened: list[int]) -> tuple[int, tuple[object, ...]]:
    """Open one child directory relative to its parent; recorded as opened before it is checked."""
    if os.name == "posix":
        child = os.open(part, _posix_directory_flags(), dir_fd=current)
        opened.append(child)
        return child, _descriptor_identity(child)
    if os.name == "nt":
        from generation_catalog import (
            _windows_handle_file_identity,
            _windows_open_relative_handle,
        )

        child = _windows_open_relative_handle(current, part, directory=True)
        opened.append(child)
        return child, _windows_handle_file_identity(child)
    raise RuntimeError("stable directory traversal is unavailable")


def _close_anchor(handle: int) -> None:
    if os.name == "nt":
        from markdown_transaction import _close_windows_handle

        _close_windows_handle(handle)
        return
    os.close(handle)


def _close_anchors(opened: list[int]) -> None:
    for opened_handle in reversed(opened):
        _close_anchor(opened_handle)


def _held_directory_entries(
    directory_anchor: int,
    *,
    max_entries: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, str]]:
    if os.name == "nt":
        return _windows_held_entries(directory_anchor, max_entries, deadline, cancelled)
    if os.name == "posix":
        return _posix_held_entries(directory_anchor, max_entries, deadline, cancelled)
    raise RuntimeError("stable directory enumeration is unavailable")


def _windows_held_entries(
    directory_anchor: int, max_entries: int, deadline: float | None, cancelled: Callable[[], bool] | None
) -> list[dict[str, str]]:
    from generation_catalog import _windows_list_directory

    entries: list[dict[str, str]] = []
    raw_entries = _windows_list_directory(directory_anchor, max_entries=max_entries)
    for name, kind in raw_entries:
        _check_stop(deadline, cancelled)
        entries.append({"name": unicodedata.normalize("NFC", name), "kind": kind})
    return entries


def _posix_held_entries(
    directory_anchor: int, max_entries: int, deadline: float | None, cancelled: Callable[[], bool] | None
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    with os.scandir(directory_anchor) as iterator:
        for entry in iterator:
            _check_stop(deadline, cancelled)
            if len(entries) >= max_entries:
                raise ValueError("repository code entry limit exceeded")
            info = os.stat(entry.name, dir_fd=directory_anchor, follow_symlinks=False)
            entries.append(
                {"name": unicodedata.normalize("NFC", entry.name), "kind": _kind(info)}
            )
    return entries


def _read_held_candidate(
    parent_anchor: int,
    name: str,
    expected_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    descriptor = _open_held_file(parent_anchor, name)
    try:
        return _read_opened_held(descriptor, expected_identity, limits, deadline, cancelled)
    finally:
        os.close(descriptor)


def _open_held_file(parent_anchor: int, name: str) -> int:
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, dir_fd=parent_anchor)
    if os.name == "nt":
        from generation_catalog import _windows_relative_file_descriptor

        return _windows_relative_file_descriptor(parent_anchor, name)
    raise RuntimeError("stable relative file open is unavailable")


def _read_opened_held(
    descriptor: int,
    expected_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    before = os.fstat(descriptor)
    before_identity = _descriptor_identity(descriptor)
    if not stat.S_ISREG(before.st_mode) or before_identity != expected_identity:
        raise PermissionError("repository code source changed before revalidation")
    if before.st_size > limits.max_file_bytes:
        raise ValueError("repository code file byte limit exceeded")
    return _stable_held_content(descriptor, before, before_identity, limits, deadline, cancelled)


def _stable_held_content(
    descriptor: int,
    before: os.stat_result,
    before_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    from generation_catalog import _stable_descriptor_state

    before_state = _stable_descriptor_state(descriptor)
    content = _bounded_content(descriptor, limits, deadline, cancelled)
    _capture_read_barrier(descriptor)
    if not _descriptor_unchanged(descriptor, before, before_identity, before_state, content):
        raise RuntimeError("repository code source changed during revalidation")
    return bytes(content), _stat_metadata(before)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    path_parts = path.split("/")
    return any(_glob_matches(path_parts, pattern.split("/")) for pattern in patterns)


def _glob_matches(path_parts: list[str], pattern_parts: list[str]) -> bool:
    """Segment-wise fnmatch where a "**" segment matches any number of path segments."""
    cursor = _GlobCursor()
    while cursor.path_index < len(path_parts):
        if not cursor.advance(path_parts, pattern_parts):
            return False
    return all(part == "**" for part in pattern_parts[cursor.pattern_index :])


def _segment_matches(pattern_part: str | None, segment: str) -> bool:
    return pattern_part is not None and pattern_part != "**" and fnmatch.fnmatchcase(segment, pattern_part)


class _GlobCursor:
    """Positions in the path and the pattern, and the last "**" to backtrack to."""

    def __init__(self) -> None:
        self.path_index = 0
        self.pattern_index = 0
        self.globstar_index = -1
        self.globstar_path_index = 0

    def advance(self, path_parts: list[str], pattern_parts: list[str]) -> bool:
        """One matching step; False when the pattern cannot match the path."""
        current = pattern_parts[self.pattern_index] if self.pattern_index < len(pattern_parts) else None
        if _segment_matches(current, path_parts[self.path_index]):
            self.path_index += 1
            self.pattern_index += 1
            return True
        if current == "**":
            self._open_globstar()
            return True
        return self._backtrack()

    def _open_globstar(self) -> None:
        self.globstar_index = self.pattern_index
        self.globstar_path_index = self.path_index
        self.pattern_index += 1

    def _backtrack(self) -> bool:
        if self.globstar_index < 0:
            return False
        self.globstar_path_index += 1
        self.path_index = self.globstar_path_index
        self.pattern_index = self.globstar_index + 1
        return True


def _file_value(item: CodeCaptureFile) -> dict[str, object]:
    return {
        "source_id": item.source_id,
        "relative_path": item.relative_path,
        "sha256": item.sha256,
        "stat": {
            name: getattr(item.stat, name)
            for name in item.stat.__dataclass_fields__
        },
    }


def _directory_value(item: DirectoryMembership) -> dict[str, object]:
    return {
        "relative_path": item.relative_path,
        "entry_count": item.entry_count,
        "entries_sha256": item.entries_sha256,
    }


def _capture_contract_dict(contract: CodeCaptureContract) -> dict[str, object]:
    return {
        "policy": {
            "roots": list(contract.policy.roots),
            "include_globs": list(contract.policy.include_globs),
            "ignore_globs": list(contract.policy.ignore_globs),
            "suffixes": list(contract.policy.suffixes),
        },
        "limits": {
            name: getattr(contract.limits, name)
            for name in contract.limits.__dataclass_fields__
        },
        "files": [_file_value(item) for item in contract.files],
        "directories": [_directory_value(item) for item in contract.directories],
        "membership_sha256": contract.membership_sha256,
    }


def code_capture_as_dict(contract: CodeCaptureContract) -> dict[str, object]:
    """Return the closed canonical manifest representation."""
    if not isinstance(contract, CodeCaptureContract):
        raise TypeError("code_capture must be a CodeCaptureContract")
    return _capture_contract_dict(contract)


def _membership_sha256(
    files: list[dict[str, object]], directories: list[dict[str, object]]
) -> str:
    return _canonical_hash({"files": files, "directories": directories})


_CAPTURE_KEYS = frozenset({"policy", "limits", "files", "directories", "membership_sha256"})
_CAPTURE_POLICY_KEYS = frozenset({"roots", "include_globs", "ignore_globs", "suffixes"})
_CAPTURE_FILE_KEYS = frozenset({"source_id", "relative_path", "sha256", "stat"})
_CAPTURE_DIRECTORY_KEYS = frozenset({"relative_path", "entry_count", "entries_sha256"})


def validate_code_capture(value: object) -> dict[str, object]:
    """Validate and normalize a closed code-capture manifest object."""
    _require_closed(value, _CAPTURE_KEYS, "code_capture must be a closed object")
    policy = _validated_capture_policy(value["policy"])
    limits = _validated_capture_limits(value["limits"])
    files = _validated_capture_files(value["files"], limits)
    directories = _validated_capture_directories(value["directories"], limits)
    _require_under_roots(files, directories, policy)
    membership = _canonical_membership(value, policy, limits, files, directories)
    contract = CodeCaptureContract(policy, limits, tuple(files), tuple(directories), membership)
    _validate_snapshot_topology(contract)
    normalized = code_capture_as_dict(contract)
    if value != normalized:
        raise ValueError("code_capture must use canonical values")
    return normalized


def _require_closed(value: object, keys: frozenset[str] | set[str], message: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(message)


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validated_capture_policy(policy_value: object) -> RepositoryCodePolicy:
    _require_closed(policy_value, _CAPTURE_POLICY_KEYS, "code_capture policy must be a closed object")
    for name in policy_value:
        _require_sorted_unique(policy_value[name], name)
    return _policy(
        policy_value["roots"],
        policy_value["include_globs"],
        policy_value["ignore_globs"],
        policy_value["suffixes"],
    )


def _require_sorted_unique(items: object, name: str) -> None:
    if not isinstance(items, list) or items != sorted(set(items)):
        raise ValueError(f"code_capture policy {name} must be sorted and unique")


def _validated_capture_limits(limits_value: object) -> RepositoryCodeLimits:
    _require_closed(
        limits_value,
        set(RepositoryCodeLimits.__dataclass_fields__),
        "code_capture limits must be a closed object",
    )
    return RepositoryCodeLimits(**limits_value)


def _capture_relative_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("code_capture paths must be strings")
    return _normalized_root(raw)


def _validated_capture_files(files_value: object, limits: RepositoryCodeLimits) -> list[CodeCaptureFile]:
    if not isinstance(files_value, list) or len(files_value) > limits.max_files:
        raise ValueError("code_capture files exceed their row ceiling")
    files = []
    total_bytes = 0
    for item in files_value:
        captured_file = _capture_file(item, limits)
        total_bytes += captured_file.stat.size
        if total_bytes > limits.max_total_bytes:
            raise ValueError("code_capture file bytes exceed their ceiling")
        files.append(captured_file)
    _require_ordered_unique_paths(files, "files")
    return files


def _capture_file(item: object, limits: RepositoryCodeLimits) -> CodeCaptureFile:
    _require_closed(item, _CAPTURE_FILE_KEYS, "code_capture files must be closed objects")
    path = _capture_relative_path(item["relative_path"])
    metadata = _capture_file_stat(item["stat"])
    if metadata.size > limits.max_file_bytes:
        raise ValueError("code_capture file size exceeds its ceiling")
    return CodeCaptureFile(item["source_id"], path, item["sha256"], metadata)


def _capture_file_stat(stat_value: object) -> FileStatMetadata:
    _require_closed(
        stat_value,
        set(FileStatMetadata.__dataclass_fields__),
        "code_capture file stat must be a closed object",
    )
    metadata_values = {
        name: stat_value[name] for name in FileStatMetadata.__dataclass_fields__
    }
    if not all(_non_negative_int(field) for field in metadata_values.values()):
        raise ValueError("code_capture file metadata must contain non-negative integers")
    return FileStatMetadata(**metadata_values)


def _validated_capture_directories(
    directories_value: object, limits: RepositoryCodeLimits
) -> list[DirectoryMembership]:
    if not isinstance(directories_value, list) or len(directories_value) > limits.max_directories:
        raise ValueError("code_capture directories exceed their row ceiling")
    directories = []
    entries = 0
    for item in directories_value:
        path, count, digest = _capture_directory_fields(item)
        entries += count
        if entries > limits.max_entries:
            raise ValueError("code_capture directory entries exceed their ceiling")
        directories.append(DirectoryMembership(path, count, _checked_directory_digest(digest)))
    _require_ordered_unique_paths(directories, "directories")
    return directories


def _capture_directory_fields(item: object) -> tuple[str, int, object]:
    _require_closed(item, _CAPTURE_DIRECTORY_KEYS, "code_capture directories must be closed objects")
    path = _capture_relative_path(item["relative_path"])
    count = item["entry_count"]
    if not _non_negative_int(count):
        raise ValueError("code_capture directory entry_count must be non-negative")
    return path, count, item["entries_sha256"]


def _checked_directory_digest(digest: object) -> str:
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("code_capture directory hash must be lowercase SHA-256")
    return digest


def _require_ordered_unique_paths(items: list, label: str) -> None:
    paths = [item.relative_path for item in items]
    if paths != sorted(paths):
        raise ValueError(f"code_capture {label} must use deterministic ordering")
    folded = [path.casefold() for path in paths]
    if len(folded) != len(set(folded)):
        raise ValueError(f"code_capture {label} contain a path collision")


def _under_any_root(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _require_under_roots(
    files: list[CodeCaptureFile], directories: list[DirectoryMembership], policy: RepositoryCodePolicy
) -> None:
    captured_paths = [item.relative_path for item in files] + [
        item.relative_path for item in directories
    ]
    if any(not _under_any_root(path, policy.roots) for path in captured_paths):
        raise ValueError("code_capture paths must remain under declared roots")


def _canonical_membership(
    value: dict,
    policy: RepositoryCodePolicy,
    limits: RepositoryCodeLimits,
    files: list[CodeCaptureFile],
    directories: list[DirectoryMembership],
) -> str:
    normalized_files = _capture_contract_dict(
        CodeCaptureContract(policy, limits, tuple(files), tuple(directories), "0" * 64)
    )["files"]
    membership = _membership_sha256(
        normalized_files,
        [_directory_value(item) for item in directories],
    )
    if value["membership_sha256"] != membership:
        raise ValueError("code_capture membership_sha256 is not canonical")
    return membership


def collect_repository_code(
    checkout_root: Path,
    *,
    roots: tuple[str, ...],
    include_globs: tuple[str, ...],
    ignore_globs: tuple[str, ...],
    suffixes: tuple[str, ...],
    limits: RepositoryCodeLimits,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CorpusSnapshot:
    """Capture exact repository bytes and a complete bounded membership contract."""
    _check_stop(deadline, cancelled)
    if not isinstance(limits, RepositoryCodeLimits):
        raise TypeError("limits must be RepositoryCodeLimits")
    with _hold_capture_root(Path(checkout_root)) as (root, root_anchor):
        _check_stop(deadline, cancelled)
        resolve_repository_scope(root, deadline=deadline, cancelled=cancelled)
        _check_stop(deadline, cancelled)
        selected_policy = _policy(roots, include_globs, ignore_globs, suffixes)
        return _collect_repository_code_from_root(
            root,
            selected_policy,
            limits,
            root_anchor=root_anchor,
            deadline=deadline,
            cancelled=cancelled,
        )


def _collect_repository_code_from_root(
    root: Path,
    selected_policy: RepositoryCodePolicy,
    limits: RepositoryCodeLimits,
    *,
    root_anchor: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> CorpusSnapshot:
    capture = _RepositoryCapture(root, selected_policy, limits, deadline, cancelled)
    for relative_root in selected_policy.roots:
        capture.capture_root(relative_root)
    _capture_final_revalidation_barrier(root)
    capture.revalidate_memberships(root_anchor)
    capture.revalidate_sources(root_anchor)
    return capture.snapshot()


def _require_plain_directory(before: os.stat_result) -> None:
    if _is_unsafe(before) or not stat.S_ISDIR(before.st_mode):
        raise PermissionError("repository code directory is a link, reparse point, or device")


def _require_inside_checkout(candidate: Path, root: Path) -> None:
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise PermissionError("repository code root escapes checkout") from exc


def _root_kind_error(info: os.stat_result) -> Exception:
    if stat.S_ISREG(info.st_mode):
        return ValueError("repository code roots must be directories")
    return PermissionError("repository code root is not a regular directory")


def _plain_kind(info: os.stat_result, relative: str) -> str:
    """"file" or "directory"; links and other objects are refused."""
    kind = _kind(info)
    if kind == "link":
        raise PermissionError(f"repository path is a link or reparse point: {relative}")
    if kind == "other":
        raise PermissionError(f"repository path is not a regular file or directory: {relative}")
    return kind


def _claimed_member_name(normalized_names: dict[str, str], name: str) -> str:
    normalized_name = unicodedata.normalize("NFC", name)
    key = normalized_name.casefold()
    if key in normalized_names and normalized_names[key] != name:
        raise ValueError("repository directory contains a normalization collision")
    normalized_names[key] = name
    return normalized_name


def _membership_entries(raw_entries: list[tuple]) -> list[dict[str, str]]:
    membership_entries = []
    normalized_names: dict[str, str] = {}
    for name, _path, info, _identity in raw_entries:
        membership_entries.append({"name": _claimed_member_name(normalized_names, name), "kind": _kind(info)})
    membership_entries.sort(key=lambda item: (item["name"], item["kind"]))
    return membership_entries


def _code_record(path: Path, relative: str, content: bytes) -> SourceRecord:
    digest = hashlib.sha256(content).hexdigest()
    source_id = f"source:{relative}"
    if len(source_id) > 512:
        raise ValueError("generated repository code source_id exceeds 512 characters")
    return SourceRecord(
        logical_id=source_id,
        relative_path=relative,
        sha256=digest,
        size=len(content),
        media_type="text/x-python" if path.suffix.casefold() == ".py" else "text/plain",
        language=language_for_path(path),
        git_oid=None,
    )


def _require_no_folded_collision(current_entries: list[dict[str, str]]) -> None:
    current_names: dict[str, str] = {}
    for entry in current_entries:
        key = entry["name"].casefold()
        if key in current_names:
            raise RuntimeError("repository code membership changed by collision")
        current_names[key] = entry["name"]


def _require_same_membership(current_entries: list[dict[str, str]], expected: DirectoryMembership) -> None:
    _require_no_folded_collision(current_entries)
    current_entries.sort(key=lambda item: (item["name"], item["kind"]))
    if (
        len(current_entries) != expected.entry_count
        or _canonical_hash(current_entries) != expected.entries_sha256
    ):
        raise RuntimeError("repository code membership changed during capture")


def _snapshot_policy(policy: RepositoryCodePolicy, limits: RepositoryCodeLimits) -> SnapshotPolicy:
    return SnapshotPolicy(
        daily_paths=(),
        code_roots=policy.roots,
        include_historical=False,
        as_of=None,
        max_files=limits.max_files,
        max_file_bytes=limits.max_file_bytes,
        max_total_bytes=limits.max_total_bytes,
        max_entries=limits.max_entries,
        max_directories=limits.max_directories,
        max_depth=limits.max_depth,
    )


def _source_chunks(source_tuple: tuple[CapturedSource, ...]) -> tuple:
    return tuple(
        chunk
        for source in source_tuple
        for chunk in canonical_retrieval_chunks(
            source_id=source.record.logical_id,
            source_path=source.record.relative_path,
            source_sha256=source.record.sha256,
            content=source.content,
        )
    )


class _RepositoryCapture:
    """One bounded walk of the selected roots, then a revalidation of all it captured."""

    def __init__(
        self,
        root: Path,
        policy: RepositoryCodePolicy,
        limits: RepositoryCodeLimits,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.root = root
        self.policy = policy
        self.limits = limits
        self.deadline = deadline
        self.cancelled = cancelled
        self.captured: list[CapturedSource] = []
        self.captured_files: list[CodeCaptureFile] = []
        self.captured_identities: dict[str, tuple[object, ...]] = {}
        self.captured_directory_identities: dict[str, tuple[object, ...]] = {}
        self.directories: list[DirectoryMembership] = []
        self.seen_names: dict[str, str] = {}
        self.entries_seen = 0
        self.total_bytes = 0

    def canonical_relative(self, path: Path) -> str:
        raw = path.relative_to(self.root).as_posix()
        normalized = unicodedata.normalize("NFC", raw)
        collision_key = normalized.casefold()
        previous = self.seen_names.get(collision_key)
        if previous is not None and previous != raw:
            raise ValueError(f"repository path normalization collision: {previous!r}, {raw!r}")
        self.seen_names[collision_key] = raw
        return normalized

    def capture_root(self, relative_root: str) -> None:
        candidate = self.root.joinpath(*PurePosixPath(relative_root).parts)
        info = candidate.lstat()
        if _is_unsafe(info):
            raise PermissionError("repository code root is a link or reparse point")
        identity = _entry_identity(candidate, info)
        _require_inside_checkout(candidate, self.root)
        self._walk_root(candidate, info, identity)

    def _walk_root(self, candidate: Path, info: os.stat_result, identity: tuple[object, ...] | None) -> None:
        if not stat.S_ISDIR(info.st_mode):
            raise _root_kind_error(info)
        if identity is None:
            raise RuntimeError("stable repository code root identity is unavailable")
        self.walk(candidate, 0, identity)

    def walk(
        self,
        directory: Path,
        depth: int,
        expected_identity: tuple[object, ...] | None = None,
    ) -> None:
        _check_stop(self.deadline, self.cancelled)
        self._require_walkable(directory, depth)
        raw_entries = self._scanned_entries(directory, expected_identity)
        self._record_membership(directory, raw_entries, expected_identity)
        for name, path, info, identity in sorted(
            raw_entries, key=lambda item: unicodedata.normalize("NFC", item[0])
        ):
            self._visit(name, path, info, identity, depth)

    def _require_walkable(self, directory: Path, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise ValueError("repository code depth limit exceeded")
        if len(self.directories) >= self.limits.max_directories:
            raise ValueError("repository code directory limit exceeded")
        _require_plain_directory(directory.lstat())

    def _scanned_entries(self, directory: Path, expected_identity: tuple[object, ...] | None) -> list[tuple]:
        raw_entries = []
        with _hold_directory_identity(directory, expected_identity):
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    raw_entries.append(self._scanned_entry(entry))
        return raw_entries

    def _scanned_entry(self, entry: os.DirEntry) -> tuple:
        _check_stop(self.deadline, self.cancelled)
        self.entries_seen += 1
        if self.entries_seen > self.limits.max_entries:
            raise ValueError("repository code entry limit exceeded")
        info = entry.stat(follow_symlinks=False)
        path = Path(entry.path)
        if os.name == "nt":
            info = path.lstat()
        _capture_entry_identity_barrier(path)
        return (entry.name, path, info, _entry_identity(path, info))

    def _record_membership(
        self, directory: Path, raw_entries: list[tuple], expected_identity: tuple[object, ...] | None
    ) -> None:
        membership_entries = _membership_entries(raw_entries)
        relative_directory = self.canonical_relative(directory)
        if expected_identity is None:
            raise RuntimeError("stable directory identity is unavailable")
        self.captured_directory_identities[relative_directory] = expected_identity
        self.directories.append(
            DirectoryMembership(
                relative_path=relative_directory,
                entry_count=len(membership_entries),
                entries_sha256=_canonical_hash(membership_entries),
            )
        )

    def _visit(
        self, name: str, path: Path, info: os.stat_result, identity: tuple[object, ...] | None, depth: int
    ) -> None:
        relative = self.canonical_relative(path)
        kind = _plain_kind(info, relative)
        ignored_directory = name.casefold() in _ALWAYS_IGNORED
        policy_ignored = _matches(relative, self.policy.ignore_globs)
        excluded = ignored_directory or policy_ignored
        if kind == "directory":
            self._descend(path, identity, depth, excluded)
            return
        if not excluded and self._selected(path, relative):
            self._capture_file(path, info, identity, relative)

    def _descend(self, path: Path, identity: tuple[object, ...] | None, depth: int, excluded: bool) -> None:
        if excluded:
            return
        if identity is None:
            raise RuntimeError("stable directory identity is unavailable")
        self.walk(path, depth + 1, identity)

    def _selected(self, path: Path, relative: str) -> bool:
        normalized_suffix = unicodedata.normalize("NFC", path.suffix).casefold()
        if self.policy.suffixes and normalized_suffix not in self.policy.suffixes:
            return False
        return not self.policy.include_globs or _matches(relative, self.policy.include_globs)

    def _capture_file(
        self, path: Path, info: os.stat_result, identity: tuple[object, ...] | None, relative: str
    ) -> None:
        if len(self.captured) >= self.limits.max_files:
            raise ValueError("repository code file limit exceeded")
        if identity is None:
            raise RuntimeError("stable file identity is unavailable")
        content, descriptor_stat = _read_candidate(
            path,
            info,
            identity,
            self.limits,
            deadline=self.deadline,
            cancelled=self.cancelled,
        )
        self._record_capture(path, relative, content, descriptor_stat, identity)

    def _record_capture(
        self,
        path: Path,
        relative: str,
        content: bytes,
        descriptor_stat: FileStatMetadata,
        identity: tuple[object, ...],
    ) -> None:
        content.decode("utf-8", errors="strict")
        self.total_bytes += len(content)
        if self.total_bytes > self.limits.max_total_bytes:
            raise ValueError("repository code total byte limit exceeded")
        record = _code_record(path, relative, content)
        self.captured.append(CapturedSource(record, SourceMetadata(type="code"), content))
        self.captured_files.append(
            CodeCaptureFile(record.logical_id, relative, record.sha256, descriptor_stat)
        )
        self.captured_identities[record.logical_id] = identity

    def revalidate_memberships(self, root_anchor: int) -> None:
        for expected in self.directories:
            _check_stop(self.deadline, self.cancelled)
            self._revalidate_membership(root_anchor, expected)

    def _revalidate_membership(self, root_anchor: int, expected: DirectoryMembership) -> None:
        try:
            current_entries = self._current_entries(root_anchor, expected.relative_path)
            _require_same_membership(current_entries, expected)
        except OSError as exc:
            raise RuntimeError("repository code membership changed during capture") from exc

    def _current_entries(self, root_anchor: int, relative_path: str) -> list[dict[str, str]]:
        with _open_captured_directory(
            root_anchor,
            relative_path,
            self.captured_directory_identities,
        ) as directory_anchor:
            return _held_directory_entries(
                directory_anchor,
                max_entries=self.limits.max_entries,
                deadline=self.deadline,
                cancelled=self.cancelled,
            )

    def revalidate_sources(self, root_anchor: int) -> None:
        for source in self.captured:
            _check_stop(self.deadline, self.cancelled)
            self._revalidate_source(root_anchor, source)

    def _revalidate_source(self, root_anchor: int, source: CapturedSource) -> None:
        relative = PurePosixPath(source.record.relative_path)
        try:
            current, current_stat = self._reread(root_anchor, relative, source)
        except OSError as exc:
            raise RuntimeError("repository code source changed during capture") from exc
        self._require_recaptured(source, current, current_stat)

    def _reread(self, root_anchor: int, relative: PurePosixPath, source: CapturedSource) -> tuple[bytes, FileStatMetadata]:
        with _open_captured_directory(
            root_anchor,
            relative.parent.as_posix(),
            self.captured_directory_identities,
        ) as parent_anchor:
            return _read_held_candidate(
                parent_anchor,
                relative.name,
                self.captured_identities[source.record.logical_id],
                self.limits,
                deadline=self.deadline,
                cancelled=self.cancelled,
            )

    def _require_recaptured(self, source: CapturedSource, current: bytes, current_stat: FileStatMetadata) -> None:
        if hashlib.sha256(current).hexdigest() != source.record.sha256:
            raise RuntimeError("repository code source changed during capture")
        if current_stat != self._captured_file(source.record.logical_id).stat:
            raise RuntimeError("repository code source changed during capture")

    def _captured_file(self, source_id: str) -> CodeCaptureFile:
        return next(item for item in self.captured_files if item.source_id == source_id)

    def snapshot(self) -> CorpusSnapshot:
        self.captured.sort(key=lambda source: source.record.relative_path)
        self.captured_files.sort(key=lambda item: item.relative_path)
        self.directories.sort(key=lambda item: item.relative_path)
        membership_hash = _membership_sha256(
            [_file_value(item) for item in self.captured_files],
            [_directory_value(item) for item in self.directories],
        )
        contract = CodeCaptureContract(
            self.policy,
            self.limits,
            tuple(self.captured_files),
            tuple(self.directories),
            membership_hash,
        )
        snapshot_policy = _snapshot_policy(self.policy, self.limits)
        source_tuple = tuple(self.captured)
        corpus_hash = canonical_source_manifest_sha256(
            (source.record for source in source_tuple), snapshot_policy
        )
        return CorpusSnapshot(
            source_tuple,
            _source_chunks(source_tuple),
            corpus_hash,
            snapshot_policy,
            code_capture=contract,
        )


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written : written + 64 * 1024])
        if count <= 0:
            raise OSError("sealed workspace write made no progress")
        written += count


def _validated_snapshot_entries(
    snapshot: CorpusSnapshot,
) -> tuple[str, tuple[tuple[str, int, str], ...]]:
    try:
        validate_code_capture(code_capture_as_dict(snapshot.code_capture))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"snapshot code capture contract is invalid: {exc}") from exc
    source_entries = _matched_source_entries(snapshot)
    manifest_sha256 = canonical_source_manifest_sha256(
        (source.record for source in snapshot.sources),
        snapshot.policy,
        collector_version=snapshot.collector_version,
        extractor_version=snapshot.extractor_version,
    )
    if manifest_sha256 != snapshot.corpus_sha256:
        raise ValueError("snapshot source manifest hash is not canonical")
    entries = tuple(
        (relative_path, size, digest)
        for _source_id, relative_path, size, digest in source_entries
    )
    return manifest_sha256, entries


def _matched_source_entries(snapshot: CorpusSnapshot) -> list[tuple[str, str, int, str]]:
    source_entries = [_checked_source_entry(source) for source in snapshot.sources]
    capture_entries = [
        (item.source_id, item.relative_path, item.stat.size, item.sha256)
        for item in snapshot.code_capture.files
    ]
    if tuple(source_entries) != tuple(capture_entries):
        raise ValueError("snapshot sources do not match the code capture contract")
    return source_entries


def _checked_source_entry(source: CapturedSource) -> tuple[str, str, int, str]:
    content = source.content
    record = source.record
    if not _source_matches_record(content, record):
        raise ValueError("snapshot source bytes do not match their record")
    return (record.logical_id, record.relative_path, record.size, record.sha256)


def _source_matches_record(content: object, record: SourceRecord) -> bool:
    relative = PurePosixPath(record.relative_path)
    if not isinstance(content, bytes) or relative.is_absolute() or _traversing_parts(relative):
        return False
    return _content_matches(content, record)


def _content_matches(content: bytes, record: SourceRecord) -> bool:
    return (
        _non_negative_int(record.size)
        and len(content) == record.size
        and hashlib.sha256(content).hexdigest() == record.sha256
    )


def _validate_snapshot_topology(contract: CodeCaptureContract) -> None:
    directory_rows = {
        item.relative_path: item.entry_count for item in contract.directories
    }
    _require_no_kind_collision(contract, directory_rows)
    _require_root_membership(contract, directory_rows)
    topology = _SnapshotTopology(contract, directory_rows)
    topology.count_directories()
    topology.count_files()
    topology.require_within_limits()


def _require_no_kind_collision(contract: CodeCaptureContract, directory_rows: dict[str, int]) -> None:
    folded_directories = {path.casefold(): path for path in directory_rows}
    folded_files = {item.relative_path.casefold(): item.relative_path for item in contract.files}
    if set(folded_directories) & set(folded_files):
        raise ValueError("snapshot code capture topology has a file/directory collision")


def _require_root_membership(contract: CodeCaptureContract, directory_rows: dict[str, int]) -> None:
    file_rows = {item.relative_path for item in contract.files}
    if any(
        root not in directory_rows and root not in file_rows
        for root in contract.policy.roots
    ):
        raise ValueError(
            "snapshot code capture topology has missing source membership "
            "for a selected root"
        )


class _SnapshotTopology:
    """Parent, depth and entry-count consistency of one capture contract."""

    def __init__(self, contract: CodeCaptureContract, directory_rows: dict[str, int]) -> None:
        self.contract = contract
        self.limits = contract.limits
        self.directory_rows = directory_rows
        self.direct_children = {path: 0 for path in directory_rows}
        self.required_directories: set[str] = set()

    def selected_root(self, path: str) -> str:
        roots = tuple(
            root
            for root in self.contract.policy.roots
            if path == root or path.startswith(root + "/")
        )
        if len(roots) != 1:
            raise ValueError("snapshot code capture topology has an ambiguous root")
        return roots[0]

    def count_directories(self) -> None:
        for path in self.directory_rows:
            self._count_directory(path)

    def _count_directory(self, path: str) -> None:
        root = self.selected_root(path)
        relative_parts = PurePosixPath(path).parts[len(PurePosixPath(root).parts) :]
        if len(relative_parts) > self.limits.max_depth:
            raise ValueError("snapshot code capture topology exceeds max_depth")
        if path != root:
            self._count_directory_parent(str(PurePosixPath(path).parent))

    def _count_directory_parent(self, parent: str) -> None:
        if parent not in self.directory_rows:
            raise ValueError("snapshot code capture topology omits a directory parent")
        self.direct_children[parent] += 1

    def count_files(self) -> None:
        for item in self.contract.files:
            self._count_file(item.relative_path)

    def _count_file(self, path: str) -> None:
        root = self.selected_root(path)
        if path == root:
            return
        parent = str(PurePosixPath(path).parent)
        self._require_file_parent(parent, root)
        self.direct_children[parent] += 1
        self._require_ancestors(PurePosixPath(parent), PurePosixPath(root))

    def _require_file_parent(self, parent: str, root: str) -> None:
        if parent not in self.directory_rows:
            raise ValueError("snapshot code capture topology omits a file parent")
        parent_depth = len(PurePosixPath(parent).parts) - len(PurePosixPath(root).parts)
        if parent_depth > self.limits.max_depth:
            raise ValueError("snapshot code capture topology exceeds max_depth")

    def _require_ancestors(self, current: PurePosixPath, root_path: PurePosixPath) -> None:
        while True:
            self.required_directories.add(current.as_posix())
            if current == root_path:
                return
            current = current.parent

    def require_within_limits(self) -> None:
        if any(self.direct_children[path] > count for path, count in self.directory_rows.items()):
            raise ValueError("snapshot code capture topology contradicts directory entry_count")
        self._require_required_bounds()

    def _require_required_bounds(self) -> None:
        if len(self.required_directories) > self.limits.max_directories:
            raise ValueError("snapshot code capture topology exceeds max_directories")
        if len(self.required_directories) + len(self.contract.files) > self.limits.max_entries:
            raise ValueError("snapshot code capture topology exceeds max_entries")


def _posix_directory_flags() -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("safe sealed workspaces require POSIX no-follow directory opens")
    _require_descriptor_relative_apis()
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_descriptor_relative_apis() -> None:
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise RuntimeError("safe sealed workspaces require descriptor-relative filesystem APIs")


def _open_posix_directory_path(path: Path) -> int:
    absolute = path.absolute()
    flags = _posix_directory_flags()
    chain = _PosixDirectoryChain(os.open(absolute.anchor, flags), flags)
    try:
        chain.descend_all(absolute.parts[1:])
    except BaseException:
        chain.abandon()
        raise
    return chain.current_descriptor()


def _require_directory_descriptor(child: int) -> None:
    child_info = os.fstat(child)
    if not stat.S_ISDIR(child_info.st_mode):
        raise PermissionError("absolute path component is not a directory")
    _descriptor_identity(child)


class _PosixDirectoryChain:
    """One open directory descriptor, moved down a path one no-follow component at a time."""

    def __init__(self, root: int, flags: int) -> None:
        self.current: int | None = root
        self.flags = flags

    def descend_all(self, parts: tuple[str, ...]) -> None:
        _descriptor_identity(self.current)
        for part in parts:
            self._descend(part)

    def _descend(self, part: str) -> None:
        child = os.open(part, self.flags, dir_fd=self.current)
        try:
            _require_directory_descriptor(child)
            self._close_current()
        except BaseException:
            os.close(child)
            raise
        self.current = child

    def _close_current(self) -> None:
        try:
            os.close(self.current)
        except BaseException:
            self.current = None
            raise

    def abandon(self) -> None:
        if self.current is not None:
            os.close(self.current)

    def current_descriptor(self) -> int:
        if self.current is None:
            raise OSError("POSIX absolute directory ownership was lost")
        return self.current


def _require_directory_room(created_directories: int, created_entries: int, limits: RepositoryCodeLimits) -> None:
    if created_directories >= limits.max_directories:
        raise ValueError("sealed workspace directory limit exceeded")
    if created_entries >= limits.max_entries:
        raise ValueError("sealed workspace entry limit exceeded")


def _require_file_counts(created_files: int, created_entries: int, limits: RepositoryCodeLimits) -> None:
    if created_files >= limits.max_files:
        raise ValueError("sealed workspace file limit exceeded")
    if created_entries >= limits.max_entries:
        raise ValueError("sealed workspace entry limit exceeded")


def _require_file_bytes(size: int, written_bytes: int, limits: RepositoryCodeLimits) -> None:
    if size > limits.max_file_bytes:
        raise ValueError("sealed workspace file byte limit exceeded")
    if written_bytes + size > limits.max_total_bytes:
        raise ValueError("sealed workspace total byte limit exceeded")


def _seal_posix(snapshot: CorpusSnapshot, destination: Path) -> None:
    parent_fd = _open_posix_directory_path(destination.parent)
    seal = _PosixSeal(snapshot.code_capture.limits)
    try:
        seal.run(snapshot, destination, parent_fd)
    finally:
        seal.close_root()
        os.close(parent_fd)


class _PosixSeal:
    """Writes one snapshot's files under a fresh owner-only root, every component held."""

    def __init__(self, limits: RepositoryCodeLimits) -> None:
        self.limits = limits
        self.root_fd: int | None = None
        self.created_directories: dict[tuple[str, ...], tuple[object, ...]] = {}
        self.created_files = 0
        self.created_entries = 0
        self.written_bytes = 0

    def run(self, snapshot: CorpusSnapshot, destination: Path, parent_fd: int) -> None:
        os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
        self.root_fd = os.open(destination.name, _posix_directory_flags(), dir_fd=parent_fd)
        for source in snapshot.sources:
            self._write_source(source)
        os.fsync(self.root_fd)
        self._require_root_unchanged(destination, parent_fd)
        os.fsync(parent_fd)

    def close_root(self) -> None:
        if self.root_fd is not None:
            os.close(self.root_fd)

    def _write_source(self, source: CapturedSource) -> None:
        parts = PurePosixPath(source.record.relative_path).parts
        chain: list[int] = []
        try:
            directory_fd = self._directory_chain(parts[:-1], chain)
            self._write_file(directory_fd, parts[-1], source)
            for opened_directory in reversed(chain):
                os.fsync(opened_directory)
        finally:
            for opened_directory in reversed(chain):
                os.close(opened_directory)

    def _directory_chain(self, parent_parts: tuple[str, ...], chain: list[int]) -> int:
        directory_fd = self.root_fd
        directory_parts: tuple[str, ...] = ()
        for depth, part in enumerate(parent_parts):
            if depth > self.limits.max_depth:
                raise ValueError("repository code depth limit exceeded")
            directory_parts = (*directory_parts, part)
            directory_fd = self._enter_directory(directory_fd, directory_parts, part, chain)
        return directory_fd

    def _enter_directory(
        self, directory_fd: int, child_parts: tuple[str, ...], part: str, chain: list[int]
    ) -> int:
        expected_identity = self.created_directories.get(child_parts)
        if expected_identity is None:
            self._create_directory(directory_fd, part)
        child_fd = os.open(part, _posix_directory_flags(), dir_fd=directory_fd)
        chain.append(child_fd)
        self._require_directory_identity(child_parts, child_fd, expected_identity)
        return child_fd

    def _create_directory(self, directory_fd: int, part: str) -> None:
        _require_directory_room(len(self.created_directories), self.created_entries, self.limits)
        _seal_component_barrier(part)
        os.mkdir(part, 0o700, dir_fd=directory_fd)
        self.created_entries += 1

    def _require_directory_identity(
        self, child_parts: tuple[str, ...], child_fd: int, expected_identity: tuple[object, ...] | None
    ) -> None:
        identity = _descriptor_identity(child_fd)
        if expected_identity is None:
            self.created_directories[child_parts] = identity
            return
        if identity != expected_identity:
            raise PermissionError("sealed workspace directory changed during creation")

    def _write_file(self, directory_fd: int, name: str, source: CapturedSource) -> None:
        _require_file_counts(self.created_files, self.created_entries, self.limits)
        _require_file_bytes(len(source.content), self.written_bytes, self.limits)
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o400,
            dir_fd=directory_fd,
        )
        try:
            _write_all(descriptor, source.content)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        self.created_files += 1
        self.created_entries += 1
        self.written_bytes += len(source.content)

    def _require_root_unchanged(self, destination: Path, parent_fd: int) -> None:
        named = os.open(destination.name, _posix_directory_flags(), dir_fd=parent_fd)
        try:
            if _descriptor_identity(named) != _descriptor_identity(self.root_fd):
                raise PermissionError("sealed workspace root changed during creation")
        finally:
            os.close(named)


def _windows_identity_record(
    relative: str, identity: tuple[int, bytes, bool]
) -> tuple[str, int, str, bool]:
    volume, file_id, directory = identity
    return relative, volume, file_id.hex(), directory


def _seal_windows(
    snapshot: CorpusSnapshot, destination: Path
) -> tuple[tuple[tuple[str, int, str, bool], ...], bool, bool]:
    parent_handle = _windows_workspace.open_directory_path(destination.parent)
    seal = _WindowsSeal(snapshot.code_capture.limits)
    try:
        seal.run(snapshot, destination, parent_handle)
    finally:
        seal.close_root()
        _windows_workspace.close_handle(parent_handle)
    return seal.records(), seal.read_only, seal.directory_flush


class _WindowsSeal:
    """Writes one snapshot's files under a fresh root through held Windows handles."""

    def __init__(self, limits: RepositoryCodeLimits) -> None:
        self.limits = limits
        self.root_handle: int | None = None
        self.identities: dict[str, tuple[int, bytes, bool]] = {}
        self.created_directories = 0
        self.created_files = 0
        self.created_entries = 0
        self.written_bytes = 0
        self.read_only = True
        self.directory_flush = True

    def run(self, snapshot: CorpusSnapshot, destination: Path, parent_handle: int) -> None:
        self.root_handle = _windows_workspace.create_directory(
            parent_handle, destination.name
        )
        self.identities[""] = _windows_workspace.identity(self.root_handle, directory=True)
        for source in snapshot.sources:
            self._write_source(source)
        self.directory_flush = (
            _windows_workspace.flush_directory(self.root_handle) and self.directory_flush
        )
        self._require_root_unchanged(parent_handle, destination.name)
        self.directory_flush = (
            _windows_workspace.flush_directory(parent_handle) and self.directory_flush
        )

    def close_root(self) -> None:
        if self.root_handle is not None:
            _windows_workspace.close_handle(self.root_handle)

    def records(self) -> tuple[tuple[str, int, str, bool], ...]:
        return tuple(
            _windows_identity_record(relative, identity)
            for relative, identity in sorted(self.identities.items())
        )

    def _write_source(self, source: CapturedSource) -> None:
        parts = PurePosixPath(source.record.relative_path).parts
        chain: list[int] = []
        try:
            directory_handle = self._directory_chain(parts[:-1], chain)
            self._write_file(directory_handle, parts[-1], source)
            self._flush_chain(chain)
        finally:
            for opened_directory in reversed(chain):
                _windows_workspace.close_handle(opened_directory)

    def _flush_chain(self, chain: list[int]) -> None:
        for opened_directory in reversed(chain):
            self.directory_flush = (
                _windows_workspace.flush_directory(opened_directory)
                and self.directory_flush
            )

    def _directory_chain(self, parent_parts: tuple[str, ...], chain: list[int]) -> int:
        directory_handle = self.root_handle
        directory_parts: tuple[str, ...] = ()
        for depth, part in enumerate(parent_parts):
            if depth > self.limits.max_depth:
                raise ValueError("repository code depth limit exceeded")
            directory_parts = (*directory_parts, part)
            directory_handle = self._enter_directory(
                directory_handle, "/".join(directory_parts), part, chain
            )
        return directory_handle

    def _enter_directory(self, directory_handle: int, relative: str, part: str, chain: list[int]) -> int:
        expected_identity = self.identities.get(relative)
        child = self._created_or_opened(directory_handle, part, expected_identity)
        chain.append(child)
        self._require_directory_identity(relative, child, expected_identity)
        return child

    def _created_or_opened(
        self, directory_handle: int, part: str, expected_identity: tuple[int, bytes, bool] | None
    ) -> int:
        if expected_identity is not None:
            return _windows_workspace.open_directory(directory_handle, part)
        _require_directory_room(self.created_directories, self.created_entries, self.limits)
        _seal_component_barrier(part)
        child = _windows_workspace.create_directory(directory_handle, part)
        self.created_directories += 1
        self.created_entries += 1
        return child

    def _require_directory_identity(
        self, relative: str, child: int, expected_identity: tuple[int, bytes, bool] | None
    ) -> None:
        current_identity = _windows_workspace.identity(child, directory=True)
        if expected_identity is None:
            self.identities[relative] = current_identity
            return
        if current_identity != expected_identity:
            raise PermissionError(
                "sealed workspace directory changed during creation"
            )

    def _write_file(self, directory_handle: int, name: str, source: CapturedSource) -> None:
        _require_file_counts(self.created_files, self.created_entries, self.limits)
        _require_file_bytes(len(source.content), self.written_bytes, self.limits)
        file_handle = _windows_workspace.create_file(directory_handle, name)
        try:
            self._fill_file(file_handle, source)
        finally:
            _windows_workspace.close_handle(file_handle)
        self.created_files += 1
        self.created_entries += 1
        self.written_bytes += len(source.content)

    def _fill_file(self, file_handle: int, source: CapturedSource) -> None:
        relative = source.record.relative_path
        before = _windows_workspace.identity(file_handle, directory=False)
        _windows_workspace.write_all(
            file_handle,
            source.content,
            chunk_bytes=self.limits.chunk_bytes,
        )
        if _windows_workspace.file_size(file_handle) != source.record.size:
            raise OSError("sealed workspace file size changed during creation")
        _windows_workspace.flush_file(file_handle)
        self._mark_read_only(file_handle)
        after = _windows_workspace.identity(file_handle, directory=False)
        if before != after:
            raise PermissionError(
                "sealed workspace file changed during creation"
            )
        self.identities[relative] = after

    def _mark_read_only(self, file_handle: int) -> None:
        applied = _windows_workspace.set_read_only(file_handle)
        if not applied:
            raise PermissionError(
                "sealed workspace read-only control is unavailable"
            )
        self.read_only = self.read_only and applied

    def _require_root_unchanged(self, parent_handle: int, name: str) -> None:
        named = _windows_workspace.open_directory(parent_handle, name)
        try:
            if _windows_workspace.identity(named, directory=True) != self.identities[""]:
                raise PermissionError("sealed workspace root changed during creation")
        finally:
            _windows_workspace.close_handle(named)


def seal_workspace(snapshot: CorpusSnapshot, root: Path) -> SealedWorkspace:
    """Write captured bytes through an exclusive component-safe filesystem boundary."""
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("seal_workspace requires a repository CorpusSnapshot")
    manifest_sha256, entries = _validated_snapshot_entries(snapshot)
    _require_sealing_support()
    destination = Path(root).absolute()
    platform_identities, read_only, directory_flush = _sealed_on_platform(snapshot, destination)
    workspace = SealedWorkspace(
        destination,
        manifest_sha256,
        entries,
        os.name == "posix",
        read_only,
        platform_identities,
        directory_flush,
    )
    verify_workspace_seal(workspace, snapshot)
    return workspace


def _require_sealing_support() -> None:
    if workspace_sealing_supported():
        return
    reason = (
        _windows_workspace.capability()[1]
        if os.name == "nt"
        else "root-relative no-follow filesystem APIs are unavailable"
    )
    raise RuntimeError(
        "sealed workspaces require a root-relative no-follow filesystem boundary: "
        + str(reason)
    )


def _sealed_on_platform(
    snapshot: CorpusSnapshot, destination: Path
) -> tuple[tuple[tuple[str, int, str, bool], ...], bool, bool]:
    """(Windows identity records, read-only applied, directory flush held) of the new seal."""
    if os.name == "nt":
        return _seal_windows(snapshot, destination)
    _seal_posix(snapshot, destination)
    return (), True, True


def _verify_descriptor_file(
    descriptor: int, size: int, digest: str, chunk_bytes: int
) -> None:
    before = os.fstat(descriptor)
    before_identity = _descriptor_identity(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise WorkspaceChanged("sealed workspace file size changed")
    total, content_digest = _bounded_digest(descriptor, size, chunk_bytes)
    _require_unchanged_descriptor(descriptor, before, before_identity)
    if total != size or content_digest != digest:
        raise WorkspaceChanged("sealed workspace file content changed")


def _bounded_digest(descriptor: int, size: int, chunk_bytes: int) -> tuple[int, str]:
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, chunk_bytes)
        if not chunk:
            return total, hasher.hexdigest()
        total += len(chunk)
        if total > size:
            raise WorkspaceChanged("sealed workspace file exceeded captured range")
        hasher.update(chunk)


def _require_unchanged_descriptor(
    descriptor: int, before: os.stat_result, before_identity: tuple[object, ...]
) -> None:
    after = os.fstat(descriptor)
    if before_identity != _descriptor_identity(descriptor) or not _same_file(before, after):
        raise WorkspaceChanged("sealed workspace file content changed")


def _posix_member_identity(
    name: str, metadata: os.stat_result
) -> tuple[str, str, int, int, int, int, int, int]:
    if metadata.st_dev <= 0 or metadata.st_ino <= 0:
        raise WorkspaceChanged("sealed workspace member identity is unavailable")
    return (
        name,
        _kind(metadata),
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _posix_directory_members(
    directory_fd: int, max_entries: int
) -> tuple[tuple[str, str, int, int, int, int, int, int], ...]:
    entries: list[tuple[str, str, int, int, int, int, int, int]] = []
    count = 0
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            count += 1
            if count > max_entries:
                raise WorkspaceChanged("sealed workspace entry range exceeded")
            metadata = os.stat(
                entry.name, dir_fd=directory_fd, follow_symlinks=False
            )
            entries.append(_posix_member_identity(entry.name, metadata))
    return tuple(sorted(entries))


def _verify_posix(
    workspace: SealedWorkspace, snapshot: CorpusSnapshot
) -> tuple[tuple[str, str], ...]:
    parent_fd = _open_posix_directory_path(workspace.root.parent)
    walk = _PosixSealWalk(snapshot)
    try:
        return walk.members(workspace, parent_fd)
    except (OSError, PermissionError) as exc:
        raise WorkspaceChanged("sealed workspace directory cannot be verified") from exc
    finally:
        walk.close()
        os.close(parent_fd)


def _require_same_member(
    entry_name: str, metadata: os.stat_result, member: tuple[str, str, int, int, int, int, int, int]
) -> None:
    if _is_unsafe(metadata):
        raise WorkspaceChanged("sealed workspace member became a link")
    if _posix_member_identity(entry_name, metadata) != member:
        raise WorkspaceChanged("sealed workspace member identity changed")


def _require_same_directory(child: int, metadata: os.stat_result) -> None:
    opened = os.fstat(child)
    if not stat.S_ISDIR(opened.st_mode) or not _same_file(metadata, opened):
        raise WorkspaceChanged(
            "sealed workspace directory identity changed before traversal"
        )


class _SealWalk:
    """A depth-first walk of a sealed workspace: frames of [prefix, depth, handle, entries, index]."""

    def __init__(self, snapshot: CorpusSnapshot) -> None:
        self.limits = snapshot.code_capture.limits
        self.expected = {
            source.record.relative_path: (
                source.record.size,
                source.record.sha256,
            )
            for source in snapshot.sources
        }
        self.stack: list[list[object]] = []
        self.members_found: list[tuple[str, str]] = []
        self.visited = 0
        self.visited_directories = 0

    def _count_directory(self, child_depth: int) -> None:
        self.visited_directories += 1
        if self.visited_directories > self.limits.max_directories:
            raise WorkspaceChanged("sealed workspace directory range exceeded")
        if child_depth > self.limits.max_depth:
            raise WorkspaceChanged("sealed workspace depth range exceeded")


class _PosixSealWalk(_SealWalk):
    def __init__(self, snapshot: CorpusSnapshot) -> None:
        super().__init__(snapshot)
        self.root_fd: int | None = None

    def members(self, workspace: SealedWorkspace, parent_fd: int) -> tuple[tuple[str, str], ...]:
        self.root_fd = os.open(
            workspace.root.name, _posix_directory_flags(), dir_fd=parent_fd
        )
        self.stack = [[(), -1, self.root_fd, None, 0]]
        while self.stack:
            self._step(self.stack[-1])
        self._require_root_unchanged(workspace, parent_fd)
        return tuple(sorted(self.members_found))

    def close(self) -> None:
        for frame in reversed(self.stack[1:]):
            os.close(frame[2])
        if self.root_fd is not None:
            os.close(self.root_fd)

    def _step(self, frame: list[object]) -> None:
        prefix, depth, directory_fd = frame[0], frame[1], frame[2]
        entries = self._frame_entries(frame)
        index = frame[4]
        if index >= len(entries):
            self._finish_frame(directory_fd, entries)
            return
        frame[4] = index + 1
        self._visit(prefix, depth, directory_fd, entries[index])

    def _frame_entries(self, frame: list[object]) -> tuple:
        if frame[3] is None:
            entries = _posix_directory_members(
                frame[2],
                self.limits.max_entries - self.visited,
            )
            self.visited += len(entries)
            frame[3] = entries
        return frame[3]

    def _finish_frame(self, directory_fd: int, entries: tuple) -> None:
        if _posix_directory_members(directory_fd, self.limits.max_entries) != entries:
            raise WorkspaceChanged(
                "sealed workspace directory membership changed"
            )
        self.stack.pop()
        if directory_fd != self.root_fd:
            os.close(directory_fd)

    def _visit(self, prefix: tuple[str, ...], depth: int, directory_fd: int, member: tuple) -> None:
        entry_name = member[0]
        _verify_component_barrier(entry_name)
        metadata = os.stat(
            entry_name, dir_fd=directory_fd, follow_symlinks=False
        )
        _require_same_member(entry_name, metadata, member)
        self._visit_member((*prefix, entry_name), depth, directory_fd, entry_name, metadata)

    def _visit_member(
        self,
        relative_parts: tuple[str, ...],
        depth: int,
        directory_fd: int,
        entry_name: str,
        metadata: os.stat_result,
    ) -> None:
        relative = "/".join(relative_parts)
        if stat.S_ISDIR(metadata.st_mode):
            self.members_found.append((relative, "directory"))
            self._enter(relative_parts, depth + 1, directory_fd, entry_name, metadata)
            return
        if stat.S_ISREG(metadata.st_mode):
            self.members_found.append((relative, "file"))
            self._verify_file(relative, directory_fd, entry_name)
            return
        raise WorkspaceChanged("sealed workspace contains a device")

    def _enter(
        self,
        relative_parts: tuple[str, ...],
        child_depth: int,
        directory_fd: int,
        entry_name: str,
        metadata: os.stat_result,
    ) -> None:
        self._count_directory(child_depth)
        child = os.open(
            entry_name, _posix_directory_flags(), dir_fd=directory_fd
        )
        try:
            _require_same_directory(child, metadata)
        except BaseException:
            os.close(child)
            raise
        self.stack.append([relative_parts, child_depth, child, None, 0])

    def _verify_file(self, relative: str, directory_fd: int, entry_name: str) -> None:
        if relative not in self.expected:
            return
        descriptor = os.open(
            entry_name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        try:
            size, digest = self.expected[relative]
            _verify_descriptor_file(
                descriptor,
                size,
                digest,
                self.limits.chunk_bytes,
            )
        finally:
            os.close(descriptor)

    def _require_root_unchanged(self, workspace: SealedWorkspace, parent_fd: int) -> None:
        named = os.open(
            workspace.root.name, _posix_directory_flags(), dir_fd=parent_fd
        )
        try:
            if _descriptor_identity(named) != _descriptor_identity(self.root_fd):
                raise WorkspaceChanged("sealed workspace root changed during verification")
        finally:
            os.close(named)


def _expected_workspace_members(
    expected: tuple[tuple[str, int, str], ...]
) -> tuple[tuple[str, str], ...]:
    members: set[tuple[str, str]] = set()
    for relative, _size, _digest in expected:
        parts = PurePosixPath(relative).parts
        for end in range(1, len(parts)):
            members.add(("/".join(parts[:end]), "directory"))
        members.add((relative, "file"))
    return tuple(sorted(members))


def _verify_windows_file(
    handle: int,
    *,
    size: int,
    digest: str,
    chunk_bytes: int,
    require_read_only: bool,
) -> None:
    before = _windows_workspace.identity(handle, directory=False)
    if _windows_workspace.file_size(handle) != size:
        raise WorkspaceChanged("sealed workspace file size changed")
    total, content_digest = _windows_digest(handle, size, chunk_bytes)
    _require_unchanged_windows_content(handle, before, size, total, content_digest == digest)
    if require_read_only and not _windows_workspace.is_read_only(handle):
        raise WorkspaceChanged("sealed workspace file is no longer read-only")


def _windows_digest(handle: int, size: int, chunk_bytes: int) -> tuple[int, str]:
    hasher = hashlib.sha256()
    total = 0
    for chunk in _windows_workspace.read_chunks(
        handle, chunk_bytes=chunk_bytes, max_bytes=size
    ):
        total += len(chunk)
        hasher.update(chunk)
    return total, hasher.hexdigest()


def _require_unchanged_windows_content(
    handle: int, before: tuple[int, bytes, bool], size: int, total: int, digest_matches: bool
) -> None:
    after = _windows_workspace.identity(handle, directory=False)
    if before != after or _windows_workspace.file_size(handle) != size:
        raise WorkspaceChanged("sealed workspace file content changed")
    if total != size or not digest_matches:
        raise WorkspaceChanged("sealed workspace file content changed")


def _validated_windows_identities(
    workspace: SealedWorkspace,
    expected_members: tuple[tuple[str, str], ...],
) -> dict[str, tuple[int, bytes, bool]]:
    identities = _decoded_windows_identities(workspace.platform_identities)
    expected_kinds = {"": True}
    expected_kinds.update(
        (relative, kind == "directory") for relative, kind in expected_members
    )
    if set(identities) != set(expected_kinds) or any(
        identities[relative][2] != directory
        for relative, directory in expected_kinds.items()
    ):
        raise WorkspaceChanged("sealed workspace identity manifest changed")
    return identities


def _decoded_windows_identities(records: tuple) -> dict[str, tuple[int, bytes, bool]]:
    identities: dict[str, tuple[int, bytes, bool]] = {}
    try:
        for record in records:
            _decode_identity_record(record, identities)
    except (TypeError, ValueError) as exc:
        raise WorkspaceChanged("sealed workspace identity manifest is invalid") from exc
    return identities


def _decode_identity_record(record: object, identities: dict[str, tuple[int, bytes, bool]]) -> None:
    relative, volume, file_id, directory = record
    if not _valid_identity_record(relative, volume, file_id, directory) or relative in identities:
        raise ValueError
    identities[relative] = (volume, bytes.fromhex(file_id), directory)


def _valid_identity_record(relative: object, volume: object, file_id: object, directory: object) -> bool:
    if not isinstance(relative, str) or not _non_negative_int(volume):
        return False
    return (
        isinstance(file_id, str)
        and _WINDOWS_FILE_ID_RE.fullmatch(file_id) is not None
        and isinstance(directory, bool)
    )


def _verify_windows(
    workspace: SealedWorkspace,
    snapshot: CorpusSnapshot,
    expected_members: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    persisted = _validated_windows_identities(workspace, expected_members)
    walk = _WindowsSealWalk(workspace, snapshot, persisted)
    try:
        return walk.members()
    except (OSError, PermissionError, ValueError) as exc:
        raise WorkspaceChanged("sealed workspace directory cannot be verified") from exc
    finally:
        walk.close()


class _WindowsSealWalk(_SealWalk):
    def __init__(
        self,
        workspace: SealedWorkspace,
        snapshot: CorpusSnapshot,
        persisted: dict[str, tuple[int, bytes, bool]],
    ) -> None:
        super().__init__(snapshot)
        self.workspace = workspace
        self.persisted = persisted
        self.parent_handle: int | None = None
        self.root_handle: int | None = None

    def members(self) -> tuple[tuple[str, str], ...]:
        self.parent_handle = _windows_workspace.open_directory_path(self.workspace.root.parent)
        self.root_handle = _windows_workspace.open_directory(
            self.parent_handle, self.workspace.root.name
        )
        if self.persisted.get("") != _windows_workspace.identity(
            self.root_handle, directory=True
        ):
            raise WorkspaceChanged("sealed workspace root identity changed")
        self.stack = [[(), -1, self.root_handle, None, 0]]
        while self.stack:
            self._step(self.stack[-1])
        self._require_root_unchanged()
        return tuple(sorted(self.members_found))

    def close(self) -> None:
        for frame in reversed(self.stack[1:]):
            _windows_workspace.close_handle(frame[2])
        _close_optional_handle(self.root_handle)
        _close_optional_handle(self.parent_handle)

    def _step(self, frame: list[object]) -> None:
        prefix, depth, directory_handle = frame[0], frame[1], frame[2]
        entries = self._frame_entries(frame)
        index = frame[4]
        if index >= len(entries):
            self._finish_frame(directory_handle, entries)
            return
        frame[4] = index + 1
        self._visit(prefix, depth, directory_handle, entries[index])

    def _frame_entries(self, frame: list[object]) -> list:
        if frame[3] is None:
            entries = _windows_workspace.list_directory(
                frame[2], max_entries=self.limits.max_entries
            )
            frame[3] = entries
            self.visited += len(entries)
            if self.visited > self.limits.max_entries:
                raise WorkspaceChanged("sealed workspace entry range exceeded")
        return frame[3]

    def _finish_frame(self, directory_handle: int, entries: list) -> None:
        if _windows_workspace.list_directory(
            directory_handle, max_entries=self.limits.max_entries
        ) != entries:
            raise WorkspaceChanged(
                "sealed workspace directory changed during verification"
            )
        self.stack.pop()
        if directory_handle != self.root_handle:
            _windows_workspace.close_handle(directory_handle)

    def _visit(self, prefix: tuple[str, ...], depth: int, directory_handle: int, entry) -> None:
        _verify_component_barrier(entry.name)
        relative_parts = (*prefix, entry.name)
        relative = "/".join(relative_parts)
        if entry.kind == "link":
            raise WorkspaceChanged("sealed workspace member became a reparse point")
        self._visit_member(relative_parts, relative, depth, directory_handle, entry)

    def _visit_member(
        self, relative_parts: tuple[str, ...], relative: str, depth: int, directory_handle: int, entry
    ) -> None:
        if entry.kind == "directory":
            self.members_found.append((relative, "directory"))
            self._enter(relative_parts, relative, depth + 1, directory_handle, entry)
            return
        if entry.kind == "file":
            self.members_found.append((relative, "file"))
            self._verify_file(relative, directory_handle, entry)
            return
        raise WorkspaceChanged("sealed workspace contains a device")

    def _enter(
        self, relative_parts: tuple[str, ...], relative: str, child_depth: int, directory_handle: int, entry
    ) -> None:
        self._count_directory(child_depth)
        child = _windows_workspace.open_directory(directory_handle, entry.name)
        try:
            self._require_directory_identity(child, relative, entry)
        except BaseException:
            _windows_workspace.close_handle(child)
            raise
        self.stack.append([relative_parts, child_depth, child, None, 0])

    def _require_directory_identity(self, child: int, relative: str, entry) -> None:
        child_identity = _windows_workspace.identity(child, directory=True)
        if (
            child_identity[1] != entry.file_id
            or (
                relative in self.persisted
                and self.persisted[relative] != child_identity
            )
        ):
            raise WorkspaceChanged(
                "sealed workspace directory identity changed"
            )

    def _verify_file(self, relative: str, directory_handle: int, entry) -> None:
        if relative not in self.expected:
            return
        handle = _windows_workspace.open_file(directory_handle, entry.name)
        try:
            self._verify_file_handle(handle, relative, entry)
        finally:
            _windows_workspace.close_handle(handle)

    def _verify_file_handle(self, handle: int, relative: str, entry) -> None:
        file_identity = _windows_workspace.identity(
            handle, directory=False
        )
        if (
            file_identity[1] != entry.file_id
            or self.persisted.get(relative) != file_identity
        ):
            raise WorkspaceChanged(
                "sealed workspace file identity changed"
            )
        size, digest = self.expected[relative]
        _verify_windows_file(
            handle,
            size=size,
            digest=digest,
            chunk_bytes=self.limits.chunk_bytes,
            require_read_only=self.workspace.read_only_requested,
        )

    def _require_root_unchanged(self) -> None:
        named = _windows_workspace.open_directory(self.parent_handle, self.workspace.root.name)
        try:
            if _windows_workspace.identity(named, directory=True) != self.persisted.get(""):
                raise WorkspaceChanged("sealed workspace root changed during verification")
        finally:
            _windows_workspace.close_handle(named)


def _close_optional_handle(handle: int | None) -> None:
    if handle is not None:
        _windows_workspace.close_handle(handle)


def verify_workspace_seal(workspace: SealedWorkspace, snapshot: CorpusSnapshot) -> bool:
    """Verify exact membership and bytes through held no-reparse components."""
    _require_verification_inputs(workspace, snapshot)
    manifest_sha256, expected = _validated_snapshot_entries(snapshot)
    if not workspace_sealing_supported():
        raise WorkspaceChanged(
            "sealed workspace verification requires a root-relative no-follow boundary"
        )
    if workspace.source_manifest_sha256 != manifest_sha256 or workspace.entries != expected:
        raise WorkspaceChanged("sealed workspace source manifest changed")
    _require_expected_members(workspace, snapshot, _expected_workspace_members(expected))
    return True


def _require_verification_inputs(workspace: object, snapshot: object) -> None:
    if not isinstance(workspace, SealedWorkspace):
        raise TypeError("workspace must be SealedWorkspace")
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("snapshot must be a repository CorpusSnapshot")


def _require_expected_members(
    workspace: SealedWorkspace, snapshot: CorpusSnapshot, expected_members: tuple[tuple[str, str], ...]
) -> None:
    actual_members = _verified_members(workspace, snapshot, expected_members)
    if actual_members != expected_members:
        raise WorkspaceChanged("sealed workspace has extra or missing entries")


def _verified_members(
    workspace: SealedWorkspace, snapshot: CorpusSnapshot, expected_members: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    if os.name == "nt":
        return _verify_windows(workspace, snapshot, expected_members)
    return _verify_posix(workspace, snapshot)


__all__ = [
    "CodeCaptureContract",
    "CodeCaptureFile",
    "DirectoryMembership",
    "FileStatMetadata",
    "RepositoryCodeLimits",
    "RepositoryCodePolicy",
    "SealedWorkspace",
    "WorkspaceChanged",
    "code_capture_as_dict",
    "collect_repository_code",
    "seal_workspace",
    "validate_code_capture",
    "verify_workspace_seal",
    "workspace_sealing_supported",
]
