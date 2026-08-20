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
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable or None")
    if cancelled is not None and cancelled():
        raise TimeoutError("repository code capture cancelled")
    if deadline is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError("repository code capture deadline reached")


def _normalized_root(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("repository code root must be path-like")
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise TypeError("repository code root must be text")
    normalized = unicodedata.normalize("NFC", raw)
    windows = PureWindowsPath(normalized)
    pure = PurePosixPath(normalized)
    if (
        not raw
        or len(raw) > _MAX_POLICY_TEXT
        or normalized == "."
        or "\\" in normalized
        or pure.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("repository code root must be a normalized relative POSIX path")
    return pure.as_posix()


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
    normalized_inputs: dict[str, str] = {}
    for value in materialized:
        raw = os.fspath(value) if isinstance(value, os.PathLike) else value
        if not isinstance(raw, str):
            continue
        normalized_input = unicodedata.normalize("NFC", raw)
        previous = normalized_inputs.get(normalized_input)
        if previous is not None and previous != raw:
            raise ValueError(f"{label} contains a Unicode normalization collision")
        normalized_inputs[normalized_input] = raw
    normalized = tuple(sorted(set(transform(value) for value in materialized)))
    return normalized


def _policy(
    roots: Iterable[str | Path],
    include_globs: Iterable[str],
    ignore_globs: Iterable[str],
    suffixes: Iterable[str],
) -> RepositoryCodePolicy:
    normalized_roots = _normalized_values(
        roots, label="roots", maximum=128, transform=_normalized_root
    )
    if not normalized_roots:
        raise ValueError("at least one repository code root is required")
    if any(
        any(part.casefold() in _ALWAYS_IGNORED for part in PurePosixPath(root).parts)
        for root in normalized_roots
    ):
        raise ValueError("repository code roots must not select always-ignored directories")
    for index, root in enumerate(normalized_roots):
        if any(other.startswith(root + "/") for other in normalized_roots[index + 1 :]):
            raise ValueError("repository code roots must not overlap")

    def pattern(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_POLICY_TEXT:
            raise ValueError("repository code glob must be a bounded non-empty string")
        normalized = unicodedata.normalize("NFC", value)
        windows = PureWindowsPath(normalized)
        pure = PurePosixPath(normalized)
        if (
            "\\" in normalized
            or pure.is_absolute()
            or windows.drive
            or windows.root
            or normalized.startswith("./")
            or "//" in normalized
            or any(part == "." for part in normalized.split("/"))
        ):
            raise ValueError("repository code glob must be normalized relative POSIX text")
        if any(part == ".." for part in pure.parts):
            raise ValueError("repository code glob must not traverse parents")
        return normalized

    def suffix(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("repository code suffix must be bounded non-empty text")
        normalized = unicodedata.normalize("NFC", value).casefold()
        if (
            len(normalized) < 2
            or not normalized.startswith(".")
            or "/" in normalized
            or "\\" in normalized
        ):
            raise ValueError("repository code suffix must begin with a dot")
        return normalized

    return RepositoryCodePolicy(
        roots=normalized_roots,
        include_globs=_normalized_values(
            include_globs, label="include_globs", maximum=256, transform=pattern
        ),
        ignore_globs=_normalized_values(
            ignore_globs, label="ignore_globs", maximum=256, transform=pattern
        ),
        suffixes=_normalized_values(
            suffixes, label="suffixes", maximum=128, transform=suffix
        ),
    )


def _is_unsafe(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _kind(info: os.stat_result) -> str:
    if _is_unsafe(info):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
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
        from generation_catalog import _windows_handle_file_identity
        from markdown_transaction import _close_windows_handle, _open_windows_directory

        handle = _open_windows_directory(path)
        try:
            try:
                identity = _windows_handle_file_identity(handle)
            except OSError as exc:
                raise RuntimeError("stable directory identity is unavailable") from exc
            if expected_identity is not None and identity != expected_identity:
                raise PermissionError("repository code directory changed before traversal")
            yield
            try:
                current = _open_windows_directory(path)
                try:
                    current_identity = _windows_handle_file_identity(current)
                finally:
                    _close_windows_handle(current)
            except OSError as exc:
                raise RuntimeError("stable directory identity is unavailable") from exc
            if identity != current_identity:
                raise RuntimeError("repository code directory changed during capture")
        finally:
            _close_windows_handle(handle)
        return
    if os.name == "posix":
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(path, flags)
        try:
            identity = _descriptor_identity(descriptor)
            if expected_identity is not None and identity != expected_identity:
                raise PermissionError("repository code directory changed before traversal")
            yield
            current = os.open(path, flags)
            try:
                if identity != _descriptor_identity(current):
                    raise RuntimeError("repository code directory changed during capture")
            finally:
                os.close(current)
        finally:
            os.close(descriptor)
        return
    raise RuntimeError("stable directory identity is unavailable")


@contextmanager
def _hold_capture_root(path: Path):
    absolute = path.absolute()
    if os.name == "posix":
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("stable checkout root identity is unavailable")
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(absolute, flags)
        except OSError as exc:
            try:
                unsafe = _is_unsafe(absolute.lstat())
            except OSError:
                unsafe = False
            if unsafe:
                raise PermissionError("checkout root is a link or reparse point") from exc
            raise
        try:
            held = os.fstat(descriptor)
            held_identity = _descriptor_identity(descriptor)
            if _is_unsafe(held) or not stat.S_ISDIR(held.st_mode):
                raise PermissionError("checkout root must be a regular non-link directory")
            _capture_root_barrier(absolute)
            resolved = absolute.resolve(strict=True)
            named = os.open(resolved, flags)
            try:
                if held_identity != _descriptor_identity(named):
                    raise PermissionError("checkout root changed during canonicalization")
            finally:
                os.close(named)
            yield resolved, descriptor
            current = os.open(absolute, flags)
            try:
                if held_identity != _descriptor_identity(current):
                    raise RuntimeError("checkout root changed during capture")
            finally:
                os.close(current)
        finally:
            os.close(descriptor)
        return
    if os.name == "nt":
        from generation_catalog import _windows_handle_file_identity
        from markdown_transaction import _close_windows_handle, _open_windows_directory

        try:
            handle = _open_windows_directory(absolute)
        except RuntimeError as exc:
            raise PermissionError("checkout root is a link or reparse point") from exc
        try:
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
            named = _open_windows_directory(resolved)
            try:
                if held_identity != _windows_handle_file_identity(named):
                    raise PermissionError("checkout root changed during canonicalization")
            finally:
                _close_windows_handle(named)
            yield resolved, handle
            current_handle = _open_windows_directory(absolute)
            try:
                if held_identity != _windows_handle_file_identity(current_handle):
                    raise RuntimeError("checkout root changed during capture")
            finally:
                _close_windows_handle(current_handle)
        finally:
            _close_windows_handle(handle)
        return
    raise RuntimeError("stable checkout root identity is unavailable")


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
        before = os.fstat(descriptor)
        before_identity = _descriptor_identity(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or expected_identity != before_identity
            or (os.name == "posix" and not _same_file(expected, before))
        ):
            raise PermissionError("repository code file changed before no-follow open")
        from generation_catalog import _stable_descriptor_state

        before_state = _stable_descriptor_state(descriptor)
        content = bytearray()
        while True:
            _check_stop(deadline, cancelled)
            chunk = os.read(descriptor, limits.chunk_bytes)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limits.max_file_bytes:
                raise ValueError("repository code file byte limit exceeded")
        _capture_read_barrier(descriptor)
        after = os.fstat(descriptor)
        if (
            before_state != _stable_descriptor_state(descriptor)
            or before_identity != _descriptor_identity(descriptor)
            or _stat_metadata(before) != _stat_metadata(after)
            or len(content) != before.st_size
        ):
            raise RuntimeError("repository code file changed during capture")
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
    finally:
        os.close(descriptor)
    return bytes(content), _stat_metadata(before)


def _entry_identity(path: Path, info: os.stat_result) -> tuple[object, ...] | None:
    kind = _kind(info)
    if kind == "file":
        descriptor = _open_read(path)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not _same_file(info, opened):
                raise PermissionError("repository code file changed during enumeration")
            return _descriptor_identity(descriptor)
        finally:
            os.close(descriptor)
    if kind == "directory":
        if os.name == "nt":
            from generation_catalog import (
                _windows_handle_file_identity,
                _windows_handle_identity_candidates,
                _windows_stat_matches_any_identity,
            )
            from markdown_transaction import _close_windows_handle, _open_windows_directory

            handle = _open_windows_directory(path)
            try:
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
                return _windows_handle_file_identity(handle)
            finally:
                _close_windows_handle(handle)
        descriptor = os.open(path, _posix_directory_flags())
        try:
            opened = os.fstat(descriptor)
            if not _same_file(info, opened):
                raise PermissionError("repository code directory changed during enumeration")
            return _descriptor_identity(descriptor)
        finally:
            os.close(descriptor)
    return None


@contextmanager
def _open_captured_directory(
    root_anchor: int,
    relative_path: str,
    captured_identities: dict[str, tuple[object, ...]],
):
    parts = () if relative_path in {"", "."} else PurePosixPath(relative_path).parts
    opened: list[int] = []
    current = root_anchor
    try:
        if not parts:
            expected = captured_identities.get(".")
            if expected is not None:
                if os.name == "posix":
                    identity = _descriptor_identity(current)
                elif os.name == "nt":
                    from generation_catalog import _windows_handle_file_identity

                    identity = _windows_handle_file_identity(current)
                else:
                    raise RuntimeError("stable directory traversal is unavailable")
                if identity != expected:
                    raise PermissionError("repository code directory changed before revalidation")
            yield current
            return
        prefix: list[str] = []
        for part in parts:
            prefix.append(part)
            expected = captured_identities.get("/".join(prefix))
            if expected is None:
                raise RuntimeError("captured directory identity is unavailable")
            if os.name == "posix":
                child = os.open(part, _posix_directory_flags(), dir_fd=current)
                opened.append(child)
                identity = _descriptor_identity(child)
            elif os.name == "nt":
                from generation_catalog import (
                    _windows_handle_file_identity,
                    _windows_open_relative_handle,
                )

                child = _windows_open_relative_handle(current, part, directory=True)
                opened.append(child)
                identity = _windows_handle_file_identity(child)
            else:
                raise RuntimeError("stable directory traversal is unavailable")
            if identity != expected:
                raise PermissionError("repository code directory changed before revalidation")
            current = child
        yield current
    finally:
        for opened_handle in reversed(opened):
            if os.name == "nt":
                from markdown_transaction import _close_windows_handle

                _close_windows_handle(opened_handle)
            else:
                os.close(opened_handle)


def _held_directory_entries(
    directory_anchor: int,
    *,
    max_entries: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if os.name == "nt":
        from generation_catalog import _windows_list_directory

        raw_entries = _windows_list_directory(directory_anchor, max_entries=max_entries)
        for name, kind in raw_entries:
            _check_stop(deadline, cancelled)
            entries.append({"name": unicodedata.normalize("NFC", name), "kind": kind})
        return entries
    if os.name == "posix":
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
    raise RuntimeError("stable directory enumeration is unavailable")


def _read_held_candidate(
    parent_anchor: int,
    name: str,
    expected_identity: tuple[object, ...],
    limits: RepositoryCodeLimits,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[bytes, FileStatMetadata]:
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=parent_anchor)
    elif os.name == "nt":
        from generation_catalog import _windows_relative_file_descriptor

        descriptor = _windows_relative_file_descriptor(parent_anchor, name)
    else:
        raise RuntimeError("stable relative file open is unavailable")
    try:
        before = os.fstat(descriptor)
        before_identity = _descriptor_identity(descriptor)
        if not stat.S_ISREG(before.st_mode) or before_identity != expected_identity:
            raise PermissionError("repository code source changed before revalidation")
        if before.st_size > limits.max_file_bytes:
            raise ValueError("repository code file byte limit exceeded")
        from generation_catalog import _stable_descriptor_state

        before_state = _stable_descriptor_state(descriptor)
        content = bytearray()
        while True:
            _check_stop(deadline, cancelled)
            chunk = os.read(descriptor, limits.chunk_bytes)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limits.max_file_bytes:
                raise ValueError("repository code file byte limit exceeded")
        _capture_read_barrier(descriptor)
        after = os.fstat(descriptor)
        if (
            before_state != _stable_descriptor_state(descriptor)
            or before_identity != _descriptor_identity(descriptor)
            or _stat_metadata(before) != _stat_metadata(after)
            or len(content) != before.st_size
        ):
            raise RuntimeError("repository code source changed during revalidation")
        return bytes(content), _stat_metadata(before)
    finally:
        os.close(descriptor)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    path_parts = path.split("/")

    def matches(pattern: str) -> bool:
        pattern_parts = pattern.split("/")
        path_index = pattern_index = 0
        globstar_index = -1
        globstar_path_index = 0
        while path_index < len(path_parts):
            if (
                pattern_index < len(pattern_parts)
                and pattern_parts[pattern_index] != "**"
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
            ):
                path_index += 1
                pattern_index += 1
            elif pattern_index < len(pattern_parts) and pattern_parts[pattern_index] == "**":
                globstar_index = pattern_index
                globstar_path_index = path_index
                pattern_index += 1
            elif globstar_index >= 0:
                globstar_path_index += 1
                path_index = globstar_path_index
                pattern_index = globstar_index + 1
            else:
                return False
        return all(part == "**" for part in pattern_parts[pattern_index:])

    return any(matches(pattern) for pattern in patterns)


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
        "files": [
            {
                "source_id": item.source_id,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "stat": {
                    name: getattr(item.stat, name)
                    for name in item.stat.__dataclass_fields__
                },
            }
            for item in contract.files
        ],
        "directories": [
            {
                "relative_path": item.relative_path,
                "entry_count": item.entry_count,
                "entries_sha256": item.entries_sha256,
            }
            for item in contract.directories
        ],
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


def validate_code_capture(value: object) -> dict[str, object]:
    """Validate and normalize a closed code-capture manifest object."""
    if not isinstance(value, dict) or set(value) != {
        "policy",
        "limits",
        "files",
        "directories",
        "membership_sha256",
    }:
        raise ValueError("code_capture must be a closed object")
    policy_value = value["policy"]
    if not isinstance(policy_value, dict) or set(policy_value) != {
        "roots",
        "include_globs",
        "ignore_globs",
        "suffixes",
    }:
        raise ValueError("code_capture policy must be a closed object")
    for name in policy_value:
        items = policy_value[name]
        if not isinstance(items, list) or items != sorted(set(items)):
            raise ValueError(f"code_capture policy {name} must be sorted and unique")
    policy = _policy(
        policy_value["roots"],
        policy_value["include_globs"],
        policy_value["ignore_globs"],
        policy_value["suffixes"],
    )
    limits_value = value["limits"]
    if not isinstance(limits_value, dict) or set(limits_value) != set(
        RepositoryCodeLimits.__dataclass_fields__
    ):
        raise ValueError("code_capture limits must be a closed object")
    limits = RepositoryCodeLimits(**limits_value)

    def relative_path(raw: object) -> str:
        if not isinstance(raw, str):
            raise ValueError("code_capture paths must be strings")
        return _normalized_root(raw)

    files_value = value["files"]
    if not isinstance(files_value, list) or len(files_value) > limits.max_files:
        raise ValueError("code_capture files exceed their row ceiling")
    files = []
    total_bytes = 0
    file_keys = {"source_id", "relative_path", "sha256", "stat"}
    for item in files_value:
        if not isinstance(item, dict) or set(item) != file_keys:
            raise ValueError("code_capture files must be closed objects")
        path = relative_path(item["relative_path"])
        source_id = item["source_id"]
        digest = item["sha256"]
        stat_value = item["stat"]
        if not isinstance(stat_value, dict) or set(stat_value) != set(
            FileStatMetadata.__dataclass_fields__
        ):
            raise ValueError("code_capture file stat must be a closed object")
        metadata_values = {
            name: stat_value[name] for name in FileStatMetadata.__dataclass_fields__
        }
        if any(isinstance(field, bool) or not isinstance(field, int) or field < 0 for field in metadata_values.values()):
            raise ValueError("code_capture file metadata must contain non-negative integers")
        metadata = FileStatMetadata(**metadata_values)
        if metadata.size > limits.max_file_bytes:
            raise ValueError("code_capture file size exceeds its ceiling")
        total_bytes += metadata.size
        if total_bytes > limits.max_total_bytes:
            raise ValueError("code_capture file bytes exceed their ceiling")
        files.append(CodeCaptureFile(source_id, path, digest, metadata))
    if [item.relative_path for item in files] != sorted(
        item.relative_path for item in files
    ):
        raise ValueError("code_capture files must use deterministic ordering")
    folded_files = [item.relative_path.casefold() for item in files]
    if len(folded_files) != len(set(folded_files)):
        raise ValueError("code_capture files contain a path collision")

    directories_value = value["directories"]
    if not isinstance(directories_value, list) or len(directories_value) > limits.max_directories:
        raise ValueError("code_capture directories exceed their row ceiling")
    directories = []
    entries = 0
    for item in directories_value:
        if not isinstance(item, dict) or set(item) != {
            "relative_path",
            "entry_count",
            "entries_sha256",
        }:
            raise ValueError("code_capture directories must be closed objects")
        path = relative_path(item["relative_path"])
        count = item["entry_count"]
        digest = item["entries_sha256"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("code_capture directory entry_count must be non-negative")
        entries += count
        if entries > limits.max_entries:
            raise ValueError("code_capture directory entries exceed their ceiling")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("code_capture directory hash must be lowercase SHA-256")
        directories.append(DirectoryMembership(path, count, digest))
    if [item.relative_path for item in directories] != sorted(
        item.relative_path for item in directories
    ):
        raise ValueError("code_capture directories must use deterministic ordering")
    folded_directories = [item.relative_path.casefold() for item in directories]
    if len(folded_directories) != len(set(folded_directories)):
        raise ValueError("code_capture directories contain a path collision")
    captured_paths = [item.relative_path for item in files] + [
        item.relative_path for item in directories
    ]
    if any(
        not any(path == root or path.startswith(root + "/") for root in policy.roots)
        for path in captured_paths
    ):
        raise ValueError("code_capture paths must remain under declared roots")
    normalized_files = _capture_contract_dict(
        CodeCaptureContract(policy, limits, tuple(files), tuple(directories), "0" * 64)
    )["files"]
    normalized_directories = [
        {
            "relative_path": item.relative_path,
            "entry_count": item.entry_count,
            "entries_sha256": item.entries_sha256,
        }
        for item in directories
    ]
    membership = _membership_sha256(
        normalized_files,
        normalized_directories,
    )
    if value["membership_sha256"] != membership:
        raise ValueError("code_capture membership_sha256 is not canonical")
    contract = CodeCaptureContract(policy, limits, tuple(files), tuple(directories), membership)
    _validate_snapshot_topology(contract)
    normalized = code_capture_as_dict(contract)
    if value != normalized:
        raise ValueError("code_capture must use canonical values")
    return normalized


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

    captured: list[CapturedSource] = []
    captured_files: list[CodeCaptureFile] = []
    captured_identities: dict[str, tuple[object, ...]] = {}
    captured_directory_identities: dict[str, tuple[object, ...]] = {}
    directories: list[DirectoryMembership] = []
    seen_names: dict[str, str] = {}
    entries_seen = 0
    total_bytes = 0

    def canonical_relative(path: Path) -> str:
        raw = path.relative_to(root).as_posix()
        normalized = unicodedata.normalize("NFC", raw)
        collision_key = normalized.casefold()
        previous = seen_names.get(collision_key)
        if previous is not None and previous != raw:
            raise ValueError(f"repository path normalization collision: {previous!r}, {raw!r}")
        seen_names[collision_key] = raw
        return normalized

    def walk(
        directory: Path,
        depth: int,
        expected_identity: tuple[object, ...] | None = None,
    ) -> None:
        nonlocal entries_seen, total_bytes
        _check_stop(deadline, cancelled)
        if depth > limits.max_depth:
            raise ValueError("repository code depth limit exceeded")
        if len(directories) >= limits.max_directories:
            raise ValueError("repository code directory limit exceeded")
        before = directory.lstat()
        if _is_unsafe(before) or not stat.S_ISDIR(before.st_mode):
            raise PermissionError("repository code directory is a link, reparse point, or device")
        raw_entries = []
        with _hold_directory_identity(directory, expected_identity):
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    _check_stop(deadline, cancelled)
                    entries_seen += 1
                    if entries_seen > limits.max_entries:
                        raise ValueError("repository code entry limit exceeded")
                    info = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    if os.name == "nt":
                        info = path.lstat()
                    _capture_entry_identity_barrier(path)
                    raw_entries.append((entry.name, path, info, _entry_identity(path, info)))
        membership_entries = []
        normalized_names: dict[str, str] = {}
        for name, _path, info, _identity in raw_entries:
            normalized_name = unicodedata.normalize("NFC", name)
            key = normalized_name.casefold()
            if key in normalized_names and normalized_names[key] != name:
                raise ValueError("repository directory contains a normalization collision")
            normalized_names[key] = name
            membership_entries.append({"name": normalized_name, "kind": _kind(info)})
        membership_entries.sort(key=lambda item: (item["name"], item["kind"]))
        relative_directory = canonical_relative(directory)
        if expected_identity is None:
            raise RuntimeError("stable directory identity is unavailable")
        captured_directory_identities[relative_directory] = expected_identity
        directories.append(
            DirectoryMembership(
                relative_path=relative_directory,
                entry_count=len(membership_entries),
                entries_sha256=_canonical_hash(membership_entries),
            )
        )
        for name, path, info, identity in sorted(
            raw_entries, key=lambda item: unicodedata.normalize("NFC", item[0])
        ):
            relative = canonical_relative(path)
            kind = _kind(info)
            if kind == "link":
                raise PermissionError(f"repository path is a link or reparse point: {relative}")
            if kind == "other":
                raise PermissionError(f"repository path is not a regular file or directory: {relative}")
            ignored_directory = name.casefold() in _ALWAYS_IGNORED
            policy_ignored = _matches(relative, selected_policy.ignore_globs)
            if kind == "directory":
                if not ignored_directory and not policy_ignored:
                    if identity is None:
                        raise RuntimeError("stable directory identity is unavailable")
                    walk(path, depth + 1, identity)
                continue
            if ignored_directory or policy_ignored:
                continue
            normalized_suffix = unicodedata.normalize("NFC", path.suffix).casefold()
            if selected_policy.suffixes and normalized_suffix not in selected_policy.suffixes:
                continue
            if selected_policy.include_globs and not _matches(relative, selected_policy.include_globs):
                continue
            if len(captured) >= limits.max_files:
                raise ValueError("repository code file limit exceeded")
            if identity is None:
                raise RuntimeError("stable file identity is unavailable")
            content, descriptor_stat = _read_candidate(
                path,
                info,
                identity,
                limits,
                deadline=deadline,
                cancelled=cancelled,
            )
            content.decode("utf-8", errors="strict")
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise ValueError("repository code total byte limit exceeded")
            digest = hashlib.sha256(content).hexdigest()
            source_id = f"source:{relative}"
            if len(source_id) > 512:
                raise ValueError("generated repository code source_id exceeds 512 characters")
            record = SourceRecord(
                logical_id=source_id,
                relative_path=relative,
                sha256=digest,
                size=len(content),
                media_type="text/x-python" if path.suffix.casefold() == ".py" else "text/plain",
                language=language_for_path(path),
                git_oid=None,
            )
            captured.append(CapturedSource(record, SourceMetadata(type="code"), content))
            captured_files.append(
                CodeCaptureFile(record.logical_id, relative, digest, descriptor_stat)
            )
            captured_identities[record.logical_id] = identity

    for relative_root in selected_policy.roots:
        candidate = root.joinpath(*PurePosixPath(relative_root).parts)
        info = candidate.lstat()
        if _is_unsafe(info):
            raise PermissionError("repository code root is a link or reparse point")
        identity = _entry_identity(candidate, info)
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PermissionError("repository code root escapes checkout") from exc
        if stat.S_ISDIR(info.st_mode):
            if identity is None:
                raise RuntimeError("stable repository code root identity is unavailable")
            walk(candidate, 0, identity)
        elif stat.S_ISREG(info.st_mode):
            raise ValueError("repository code roots must be directories")
        else:
            raise PermissionError("repository code root is not a regular directory")

    _capture_final_revalidation_barrier(root)
    for expected in directories:
        _check_stop(deadline, cancelled)
        try:
            with _open_captured_directory(
                root_anchor,
                expected.relative_path,
                captured_directory_identities,
            ) as directory_anchor:
                current_entries = _held_directory_entries(
                    directory_anchor,
                    max_entries=limits.max_entries,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            current_names: dict[str, str] = {}
            for entry in current_entries:
                name = entry["name"]
                key = name.casefold()
                if key in current_names:
                    raise RuntimeError("repository code membership changed by collision")
                current_names[key] = name
            current_entries.sort(key=lambda item: (item["name"], item["kind"]))
            if (
                len(current_entries) != expected.entry_count
                or _canonical_hash(current_entries) != expected.entries_sha256
            ):
                raise RuntimeError("repository code membership changed during capture")
        except OSError as exc:
            raise RuntimeError("repository code membership changed during capture") from exc

    for source in captured:
        _check_stop(deadline, cancelled)
        relative = PurePosixPath(source.record.relative_path)
        parent_relative = relative.parent.as_posix()
        try:
            with _open_captured_directory(
                root_anchor,
                parent_relative,
                captured_directory_identities,
            ) as parent_anchor:
                current, current_stat = _read_held_candidate(
                    parent_anchor,
                    relative.name,
                    captured_identities[source.record.logical_id],
                    limits,
                    deadline=deadline,
                    cancelled=cancelled,
                )
        except OSError as exc:
            raise RuntimeError("repository code source changed during capture") from exc
        if hashlib.sha256(current).hexdigest() != source.record.sha256:
            raise RuntimeError("repository code source changed during capture")
        original_file = next(
            item for item in captured_files if item.source_id == source.record.logical_id
        )
        if current_stat != original_file.stat:
            raise RuntimeError("repository code source changed during capture")

    captured.sort(key=lambda source: source.record.relative_path)
    captured_files.sort(key=lambda item: item.relative_path)
    directories.sort(key=lambda item: item.relative_path)
    directory_values = [
        {
            "relative_path": item.relative_path,
            "entry_count": item.entry_count,
            "entries_sha256": item.entries_sha256,
        }
        for item in directories
    ]
    file_values = [
        {
            "source_id": item.source_id,
            "relative_path": item.relative_path,
            "sha256": item.sha256,
            "stat": {
                name: getattr(item.stat, name)
                for name in item.stat.__dataclass_fields__
            },
        }
        for item in captured_files
    ]
    membership_hash = _membership_sha256(
        file_values,
        directory_values,
    )
    contract = CodeCaptureContract(
        selected_policy,
        limits,
        tuple(captured_files),
        tuple(directories),
        membership_hash,
    )
    snapshot_policy = SnapshotPolicy(
        daily_paths=(),
        code_roots=selected_policy.roots,
        include_historical=False,
        as_of=None,
        max_files=limits.max_files,
        max_file_bytes=limits.max_file_bytes,
        max_total_bytes=limits.max_total_bytes,
        max_entries=limits.max_entries,
        max_directories=limits.max_directories,
        max_depth=limits.max_depth,
    )
    source_tuple = tuple(captured)
    chunks = tuple(
        chunk
        for source in source_tuple
        for chunk in canonical_retrieval_chunks(
            source_id=source.record.logical_id,
            source_path=source.record.relative_path,
            source_sha256=source.record.sha256,
            content=source.content,
        )
    )
    corpus_hash = canonical_source_manifest_sha256(
        (source.record for source in source_tuple), snapshot_policy
    )
    return CorpusSnapshot(
        source_tuple,
        chunks,
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
    source_entries = []
    capture_entries = []
    for source in snapshot.sources:
        content = source.content
        record = source.record
        relative = PurePosixPath(record.relative_path)
        if (
            not isinstance(content, bytes)
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or isinstance(record.size, bool)
            or not isinstance(record.size, int)
            or record.size < 0
            or len(content) != record.size
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            raise ValueError("snapshot source bytes do not match their record")
        source_entries.append(
            (record.logical_id, record.relative_path, record.size, record.sha256)
        )
    for item in snapshot.code_capture.files:
        capture_entries.append(
            (item.source_id, item.relative_path, item.stat.size, item.sha256)
        )
    if tuple(source_entries) != tuple(capture_entries):
        raise ValueError("snapshot sources do not match the code capture contract")
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


def _validate_snapshot_topology(contract: CodeCaptureContract) -> None:
    limits = contract.limits
    directory_rows = {
        item.relative_path: item.entry_count for item in contract.directories
    }
    folded_directories = {path.casefold(): path for path in directory_rows}
    folded_files = {item.relative_path.casefold(): item.relative_path for item in contract.files}
    if set(folded_directories) & set(folded_files):
        raise ValueError("snapshot code capture topology has a file/directory collision")
    file_rows = {item.relative_path for item in contract.files}
    if any(
        root not in directory_rows and root not in file_rows
        for root in contract.policy.roots
    ):
        raise ValueError(
            "snapshot code capture topology has missing source membership "
            "for a selected root"
        )

    def selected_root(path: str) -> str:
        roots = tuple(
            root
            for root in contract.policy.roots
            if path == root or path.startswith(root + "/")
        )
        if len(roots) != 1:
            raise ValueError("snapshot code capture topology has an ambiguous root")
        return roots[0]

    direct_children = {path: 0 for path in directory_rows}
    required_directories: set[str] = set()
    for path in directory_rows:
        root = selected_root(path)
        relative_parts = PurePosixPath(path).parts[len(PurePosixPath(root).parts) :]
        if len(relative_parts) > limits.max_depth:
            raise ValueError("snapshot code capture topology exceeds max_depth")
        if path != root:
            parent = str(PurePosixPath(path).parent)
            if parent not in directory_rows:
                raise ValueError("snapshot code capture topology omits a directory parent")
            direct_children[parent] += 1

    for item in contract.files:
        path = item.relative_path
        root = selected_root(path)
        if path == root:
            continue
        parent = str(PurePosixPath(path).parent)
        if parent not in directory_rows:
            raise ValueError("snapshot code capture topology omits a file parent")
        parent_depth = len(PurePosixPath(parent).parts) - len(PurePosixPath(root).parts)
        if parent_depth > limits.max_depth:
            raise ValueError("snapshot code capture topology exceeds max_depth")
        direct_children[parent] += 1
        current = PurePosixPath(parent)
        root_path = PurePosixPath(root)
        while True:
            required_directories.add(current.as_posix())
            if current == root_path:
                break
            current = current.parent

    if any(direct_children[path] > count for path, count in directory_rows.items()):
        raise ValueError("snapshot code capture topology contradicts directory entry_count")
    if len(required_directories) > limits.max_directories:
        raise ValueError("snapshot code capture topology exceeds max_directories")
    if len(required_directories) + len(contract.files) > limits.max_entries:
        raise ValueError("snapshot code capture topology exceeds max_entries")


def _posix_directory_flags() -> int:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("safe sealed workspaces require POSIX no-follow directory opens")
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise RuntimeError("safe sealed workspaces require descriptor-relative filesystem APIs")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_posix_directory_path(path: Path) -> int:
    absolute = path.absolute()
    flags = _posix_directory_flags()
    current: int | None = os.open(absolute.anchor, flags)
    try:
        _descriptor_identity(current)
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            child_owned = True
            try:
                child_info = os.fstat(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise PermissionError("absolute path component is not a directory")
                _descriptor_identity(child)
                try:
                    os.close(current)
                except BaseException:
                    current = None
                    raise
                current = child
                child_owned = False
            finally:
                if child_owned:
                    os.close(child)
        if current is None:
            raise OSError("POSIX absolute directory ownership was lost")
        return current
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def _seal_posix(snapshot: CorpusSnapshot, destination: Path) -> None:
    parent_fd = _open_posix_directory_path(destination.parent)
    root_fd: int | None = None
    created_directories: dict[tuple[str, ...], tuple[object, ...]] = {}
    created_files = 0
    created_entries = 0
    written_bytes = 0
    limits = snapshot.code_capture.limits
    try:
        os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
        root_fd = os.open(destination.name, _posix_directory_flags(), dir_fd=parent_fd)
        for source in snapshot.sources:
            parts = PurePosixPath(source.record.relative_path).parts
            chain: list[int] = []
            directory_fd = root_fd
            directory_parts: tuple[str, ...] = ()
            try:
                for depth, part in enumerate(parts[:-1]):
                    if depth > snapshot.code_capture.limits.max_depth:
                        raise ValueError("repository code depth limit exceeded")
                    child_parts = (*directory_parts, part)
                    expected_identity = created_directories.get(child_parts)
                    if expected_identity is None:
                        if len(created_directories) >= limits.max_directories:
                            raise ValueError("sealed workspace directory limit exceeded")
                        if created_entries >= limits.max_entries:
                            raise ValueError("sealed workspace entry limit exceeded")
                        _seal_component_barrier(part)
                        os.mkdir(part, 0o700, dir_fd=directory_fd)
                        created_entries += 1
                    directory_fd = os.open(
                        part, _posix_directory_flags(), dir_fd=directory_fd
                    )
                    chain.append(directory_fd)
                    identity = _descriptor_identity(directory_fd)
                    if expected_identity is None:
                        created_directories[child_parts] = identity
                    elif identity != expected_identity:
                        raise PermissionError(
                            "sealed workspace directory changed during creation"
                        )
                    directory_parts = child_parts
                if created_files >= limits.max_files:
                    raise ValueError("sealed workspace file limit exceeded")
                if created_entries >= limits.max_entries:
                    raise ValueError("sealed workspace entry limit exceeded")
                if len(source.content) > limits.max_file_bytes:
                    raise ValueError("sealed workspace file byte limit exceeded")
                if written_bytes + len(source.content) > limits.max_total_bytes:
                    raise ValueError("sealed workspace total byte limit exceeded")
                descriptor = os.open(
                    parts[-1],
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
                created_files += 1
                created_entries += 1
                written_bytes += len(source.content)
                for opened_directory in reversed(chain):
                    os.fsync(opened_directory)
            finally:
                for opened_directory in reversed(chain):
                    os.close(opened_directory)
        os.fsync(root_fd)
        named = os.open(destination.name, _posix_directory_flags(), dir_fd=parent_fd)
        try:
            if _descriptor_identity(named) != _descriptor_identity(root_fd):
                raise PermissionError("sealed workspace root changed during creation")
        finally:
            os.close(named)
        os.fsync(parent_fd)
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _windows_identity_record(
    relative: str, identity: tuple[int, bytes, bool]
) -> tuple[str, int, str, bool]:
    volume, file_id, directory = identity
    return relative, volume, file_id.hex(), directory


def _seal_windows(
    snapshot: CorpusSnapshot, destination: Path
) -> tuple[tuple[tuple[str, int, str, bool], ...], bool, bool]:
    parent_handle = _windows_workspace.open_directory_path(destination.parent)
    root_handle: int | None = None
    identities: dict[str, tuple[int, bytes, bool]] = {}
    created_directories = 0
    created_files = 0
    created_entries = 0
    written_bytes = 0
    read_only = True
    directory_flush = True
    limits = snapshot.code_capture.limits
    try:
        root_handle = _windows_workspace.create_directory(
            parent_handle, destination.name
        )
        identities[""] = _windows_workspace.identity(root_handle, directory=True)
        for source in snapshot.sources:
            parts = PurePosixPath(source.record.relative_path).parts
            chain: list[int] = []
            directory_handle = root_handle
            directory_parts: tuple[str, ...] = ()
            try:
                for depth, part in enumerate(parts[:-1]):
                    if depth > limits.max_depth:
                        raise ValueError("repository code depth limit exceeded")
                    child_parts = (*directory_parts, part)
                    relative = "/".join(child_parts)
                    expected_identity = identities.get(relative)
                    if expected_identity is None:
                        if created_directories >= limits.max_directories:
                            raise ValueError("sealed workspace directory limit exceeded")
                        if created_entries >= limits.max_entries:
                            raise ValueError("sealed workspace entry limit exceeded")
                        _seal_component_barrier(part)
                        child = _windows_workspace.create_directory(
                            directory_handle, part
                        )
                        created_directories += 1
                        created_entries += 1
                    else:
                        child = _windows_workspace.open_directory(directory_handle, part)
                    chain.append(child)
                    current_identity = _windows_workspace.identity(child, directory=True)
                    if expected_identity is None:
                        identities[relative] = current_identity
                    elif current_identity != expected_identity:
                        raise PermissionError(
                            "sealed workspace directory changed during creation"
                        )
                    directory_handle = child
                    directory_parts = child_parts

                if created_files >= limits.max_files:
                    raise ValueError("sealed workspace file limit exceeded")
                if created_entries >= limits.max_entries:
                    raise ValueError("sealed workspace entry limit exceeded")
                if len(source.content) > limits.max_file_bytes:
                    raise ValueError("sealed workspace file byte limit exceeded")
                if written_bytes + len(source.content) > limits.max_total_bytes:
                    raise ValueError("sealed workspace total byte limit exceeded")
                file_handle = _windows_workspace.create_file(
                    directory_handle, parts[-1]
                )
                try:
                    relative = source.record.relative_path
                    before = _windows_workspace.identity(file_handle, directory=False)
                    _windows_workspace.write_all(
                        file_handle,
                        source.content,
                        chunk_bytes=limits.chunk_bytes,
                    )
                    if _windows_workspace.file_size(file_handle) != source.record.size:
                        raise OSError("sealed workspace file size changed during creation")
                    _windows_workspace.flush_file(file_handle)
                    applied = _windows_workspace.set_read_only(file_handle)
                    if not applied:
                        raise PermissionError(
                            "sealed workspace read-only control is unavailable"
                        )
                    read_only = read_only and applied
                    after = _windows_workspace.identity(file_handle, directory=False)
                    if before != after:
                        raise PermissionError(
                            "sealed workspace file changed during creation"
                        )
                    identities[relative] = after
                finally:
                    _windows_workspace.close_handle(file_handle)
                created_files += 1
                created_entries += 1
                written_bytes += len(source.content)
                for opened_directory in reversed(chain):
                    directory_flush = (
                        _windows_workspace.flush_directory(opened_directory)
                        and directory_flush
                    )
            finally:
                for opened_directory in reversed(chain):
                    _windows_workspace.close_handle(opened_directory)

        directory_flush = (
            _windows_workspace.flush_directory(root_handle) and directory_flush
        )
        named = _windows_workspace.open_directory(
            parent_handle, destination.name
        )
        try:
            if _windows_workspace.identity(named, directory=True) != identities[""]:
                raise PermissionError("sealed workspace root changed during creation")
        finally:
            _windows_workspace.close_handle(named)
        directory_flush = (
            _windows_workspace.flush_directory(parent_handle) and directory_flush
        )
    finally:
        if root_handle is not None:
            _windows_workspace.close_handle(root_handle)
        _windows_workspace.close_handle(parent_handle)
    records = tuple(
        _windows_identity_record(relative, identity)
        for relative, identity in sorted(identities.items())
    )
    return records, read_only, directory_flush


def seal_workspace(snapshot: CorpusSnapshot, root: Path) -> SealedWorkspace:
    """Write captured bytes through an exclusive component-safe filesystem boundary."""
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("seal_workspace requires a repository CorpusSnapshot")
    manifest_sha256, entries = _validated_snapshot_entries(snapshot)
    if not workspace_sealing_supported():
        reason = (
            _windows_workspace.capability()[1]
            if os.name == "nt"
            else "root-relative no-follow filesystem APIs are unavailable"
        )
        raise RuntimeError(
            "sealed workspaces require a root-relative no-follow filesystem boundary: "
            + str(reason)
        )
    destination = Path(root).absolute()
    platform_identities: tuple[tuple[str, int, str, bool], ...] = ()
    directory_flush = True
    read_only = True
    if os.name == "nt":
        platform_identities, read_only, directory_flush = _seal_windows(
            snapshot, destination
        )
    else:
        _seal_posix(snapshot, destination)
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


def _verify_descriptor_file(
    descriptor: int, size: int, digest: str, chunk_bytes: int
) -> None:
    before = os.fstat(descriptor)
    before_identity = _descriptor_identity(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise WorkspaceChanged("sealed workspace file size changed")
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > size:
            raise WorkspaceChanged("sealed workspace file exceeded captured range")
        hasher.update(chunk)
    after = os.fstat(descriptor)
    if (
        before_identity != _descriptor_identity(descriptor)
        or not _same_file(before, after)
        or total != size
        or hasher.hexdigest() != digest
    ):
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
    root_fd: int | None = None
    members = []
    visited = 0
    visited_directories = 0
    expected = {
        source.record.relative_path: (
            source.record.size,
            source.record.sha256,
        )
        for source in snapshot.sources
    }
    try:
        root_fd = os.open(
            workspace.root.name, _posix_directory_flags(), dir_fd=parent_fd
        )
        stack: list[list[object]] = [[(), -1, root_fd, None, 0]]
        while stack:
            frame = stack[-1]
            prefix = frame[0]
            depth = frame[1]
            directory_fd = frame[2]
            entries = frame[3]
            if entries is None:
                entries = _posix_directory_members(
                    directory_fd,
                    snapshot.code_capture.limits.max_entries - visited,
                )
                visited += len(entries)
                frame[3] = entries
            index = frame[4]
            if index >= len(entries):
                if _posix_directory_members(
                    directory_fd, snapshot.code_capture.limits.max_entries
                ) != entries:
                    raise WorkspaceChanged(
                        "sealed workspace directory membership changed"
                    )
                stack.pop()
                if directory_fd != root_fd:
                    os.close(directory_fd)
                continue
            entry_name = entries[index][0]
            frame[4] = index + 1
            _verify_component_barrier(entry_name)
            metadata = os.stat(
                entry_name, dir_fd=directory_fd, follow_symlinks=False
            )
            if _is_unsafe(metadata):
                raise WorkspaceChanged("sealed workspace member became a link")
            if _posix_member_identity(entry_name, metadata) != entries[index]:
                raise WorkspaceChanged("sealed workspace member identity changed")
            relative_parts = (*prefix, entry_name)
            relative = "/".join(relative_parts)
            if stat.S_ISDIR(metadata.st_mode):
                members.append((relative, "directory"))
                visited_directories += 1
                if visited_directories > snapshot.code_capture.limits.max_directories:
                    raise WorkspaceChanged("sealed workspace directory range exceeded")
                child_depth = depth + 1
                if child_depth > snapshot.code_capture.limits.max_depth:
                    raise WorkspaceChanged("sealed workspace depth range exceeded")
                child = os.open(
                    entry_name, _posix_directory_flags(), dir_fd=directory_fd
                )
                child_owned = True
                try:
                    opened = os.fstat(child)
                    if not stat.S_ISDIR(opened.st_mode) or not _same_file(metadata, opened):
                        raise WorkspaceChanged(
                            "sealed workspace directory identity changed before traversal"
                        )
                    stack.append([relative_parts, child_depth, child, None, 0])
                    child_owned = False
                finally:
                    if child_owned:
                        os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                members.append((relative, "file"))
                if relative in expected:
                    descriptor = os.open(
                        entry_name,
                        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        size, digest = expected[relative]
                        _verify_descriptor_file(
                            descriptor,
                            size,
                            digest,
                            snapshot.code_capture.limits.chunk_bytes,
                        )
                    finally:
                        os.close(descriptor)
            else:
                raise WorkspaceChanged("sealed workspace contains a device")
        named = os.open(
            workspace.root.name, _posix_directory_flags(), dir_fd=parent_fd
        )
        try:
            if _descriptor_identity(named) != _descriptor_identity(root_fd):
                raise WorkspaceChanged("sealed workspace root changed during verification")
        finally:
            os.close(named)
        return tuple(sorted(members))
    except (OSError, PermissionError) as exc:
        raise WorkspaceChanged("sealed workspace directory cannot be verified") from exc
    finally:
        if "stack" in locals():
            for frame in reversed(stack[1:]):
                os.close(frame[2])
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


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
    hasher = hashlib.sha256()
    total = 0
    for chunk in _windows_workspace.read_chunks(
        handle, chunk_bytes=chunk_bytes, max_bytes=size
    ):
        total += len(chunk)
        hasher.update(chunk)
    after = _windows_workspace.identity(handle, directory=False)
    if (
        before != after
        or _windows_workspace.file_size(handle) != size
        or total != size
        or hasher.hexdigest() != digest
    ):
        raise WorkspaceChanged("sealed workspace file content changed")
    if require_read_only and not _windows_workspace.is_read_only(handle):
        raise WorkspaceChanged("sealed workspace file is no longer read-only")


def _validated_windows_identities(
    workspace: SealedWorkspace,
    expected_members: tuple[tuple[str, str], ...],
) -> dict[str, tuple[int, bytes, bool]]:
    identities: dict[str, tuple[int, bytes, bool]] = {}
    try:
        for relative, volume, file_id, directory in workspace.platform_identities:
            if (
                not isinstance(relative, str)
                or isinstance(volume, bool)
                or not isinstance(volume, int)
                or volume < 0
                or not isinstance(file_id, str)
                or _WINDOWS_FILE_ID_RE.fullmatch(file_id) is None
                or not isinstance(directory, bool)
                or relative in identities
            ):
                raise ValueError
            decoded = bytes.fromhex(file_id)
            identities[relative] = (volume, decoded, directory)
    except (TypeError, ValueError) as exc:
        raise WorkspaceChanged("sealed workspace identity manifest is invalid") from exc
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


def _verify_windows(
    workspace: SealedWorkspace,
    snapshot: CorpusSnapshot,
    expected_members: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    persisted = _validated_windows_identities(workspace, expected_members)
    parent_handle: int | None = None
    root_handle: int | None = None
    members: list[tuple[str, str]] = []
    visited = 0
    visited_directories = 0
    expected = {
        source.record.relative_path: (source.record.size, source.record.sha256)
        for source in snapshot.sources
    }
    limits = snapshot.code_capture.limits
    try:
        parent_handle = _windows_workspace.open_directory_path(workspace.root.parent)
        root_handle = _windows_workspace.open_directory(
            parent_handle, workspace.root.name
        )
        if persisted.get("") != _windows_workspace.identity(
            root_handle, directory=True
        ):
            raise WorkspaceChanged("sealed workspace root identity changed")
        stack: list[list[object]] = [[(), -1, root_handle, None, 0]]
        while stack:
            frame = stack[-1]
            prefix = frame[0]
            depth = frame[1]
            directory_handle = frame[2]
            entries = frame[3]
            if entries is None:
                entries = _windows_workspace.list_directory(
                    directory_handle, max_entries=limits.max_entries
                )
                frame[3] = entries
                visited += len(entries)
                if visited > limits.max_entries:
                    raise WorkspaceChanged("sealed workspace entry range exceeded")
            index = frame[4]
            if index >= len(entries):
                if _windows_workspace.list_directory(
                    directory_handle, max_entries=limits.max_entries
                ) != entries:
                    raise WorkspaceChanged(
                        "sealed workspace directory changed during verification"
                    )
                stack.pop()
                if directory_handle != root_handle:
                    _windows_workspace.close_handle(directory_handle)
                continue
            entry = entries[index]
            frame[4] = index + 1
            _verify_component_barrier(entry.name)
            relative_parts = (*prefix, entry.name)
            relative = "/".join(relative_parts)
            if entry.kind == "link":
                raise WorkspaceChanged("sealed workspace member became a reparse point")
            if entry.kind == "directory":
                members.append((relative, "directory"))
                visited_directories += 1
                if visited_directories > limits.max_directories:
                    raise WorkspaceChanged("sealed workspace directory range exceeded")
                child_depth = depth + 1
                if child_depth > limits.max_depth:
                    raise WorkspaceChanged("sealed workspace depth range exceeded")
                child = _windows_workspace.open_directory(directory_handle, entry.name)
                try:
                    child_identity = _windows_workspace.identity(child, directory=True)
                    if (
                        child_identity[1] != entry.file_id
                        or (
                            relative in persisted
                            and persisted[relative] != child_identity
                        )
                    ):
                        raise WorkspaceChanged(
                            "sealed workspace directory identity changed"
                        )
                except BaseException:
                    _windows_workspace.close_handle(child)
                    raise
                stack.append([relative_parts, child_depth, child, None, 0])
            elif entry.kind == "file":
                members.append((relative, "file"))
                if relative in expected:
                    handle = _windows_workspace.open_file(directory_handle, entry.name)
                    try:
                        file_identity = _windows_workspace.identity(
                            handle, directory=False
                        )
                        if (
                            file_identity[1] != entry.file_id
                            or persisted.get(relative) != file_identity
                        ):
                            raise WorkspaceChanged(
                                "sealed workspace file identity changed"
                            )
                        size, digest = expected[relative]
                        _verify_windows_file(
                            handle,
                            size=size,
                            digest=digest,
                            chunk_bytes=limits.chunk_bytes,
                            require_read_only=workspace.read_only_requested,
                        )
                    finally:
                        _windows_workspace.close_handle(handle)
            else:
                raise WorkspaceChanged("sealed workspace contains a device")

        named = _windows_workspace.open_directory(parent_handle, workspace.root.name)
        try:
            if _windows_workspace.identity(named, directory=True) != persisted.get(""):
                raise WorkspaceChanged("sealed workspace root changed during verification")
        finally:
            _windows_workspace.close_handle(named)
        return tuple(sorted(members))
    except (OSError, PermissionError, ValueError) as exc:
        raise WorkspaceChanged("sealed workspace directory cannot be verified") from exc
    finally:
        if "stack" in locals():
            for frame in reversed(stack[1:]):
                _windows_workspace.close_handle(frame[2])
        if root_handle is not None:
            _windows_workspace.close_handle(root_handle)
        if parent_handle is not None:
            _windows_workspace.close_handle(parent_handle)


def verify_workspace_seal(workspace: SealedWorkspace, snapshot: CorpusSnapshot) -> bool:
    """Verify exact membership and bytes through held no-reparse components."""
    if not isinstance(workspace, SealedWorkspace):
        raise TypeError("workspace must be SealedWorkspace")
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("snapshot must be a repository CorpusSnapshot")
    manifest_sha256, expected = _validated_snapshot_entries(snapshot)
    if not workspace_sealing_supported():
        raise WorkspaceChanged(
            "sealed workspace verification requires a root-relative no-follow boundary"
        )
    if workspace.source_manifest_sha256 != manifest_sha256 or workspace.entries != expected:
        raise WorkspaceChanged("sealed workspace source manifest changed")
    expected_members = _expected_workspace_members(expected)
    actual_members = (
        _verify_windows(workspace, snapshot, expected_members)
        if os.name == "nt"
        else _verify_posix(workspace, snapshot)
    )
    if actual_members != expected_members:
        raise WorkspaceChanged("sealed workspace has extra or missing entries")
    return True


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
