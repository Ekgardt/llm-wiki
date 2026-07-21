"""Bounded repository capture and sealed analyzer workspaces."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from code_languages import language_for_path
from corpus_snapshot import (
    CapturedSource,
    CodeCaptureContract,
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
from reliable_memory import canonical_json_bytes, fsync_directory
from repository_scope import resolve_repository_scope

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ALWAYS_IGNORED = frozenset(
    {".git", ".venv", "venv", "env", "cache", "logs", "run", "__pycache__"}
)
_MAX_POLICY_ITEMS = 256
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


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_stop(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    if bool(cancelled and cancelled()):
        raise TimeoutError("repository code capture cancelled")
    if deadline is not None:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise ValueError("deadline must be a monotonic timestamp")
        if time.monotonic() >= deadline:
            raise TimeoutError("repository code capture deadline reached")


def _normalized_root(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("repository code root must be path-like")
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise TypeError("repository code root must be text")
    windows = PureWindowsPath(raw)
    pure = PurePosixPath(raw)
    normalized = unicodedata.normalize("NFC", raw)
    if (
        not raw
        or raw == "."
        or len(raw) > _MAX_POLICY_TEXT
        or normalized != raw
        or "\\" in raw
        or pure.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("repository code root must be a normalized relative POSIX path")
    return pure.as_posix()


def _normalized_values(
    values: Iterable[object], *, label: str, transform: Callable[[object], str]
) -> tuple[str, ...]:
    materialized = tuple(values)
    if len(materialized) > _MAX_POLICY_ITEMS:
        raise ValueError(f"{label} has too many entries")
    normalized = tuple(sorted(set(transform(value) for value in materialized)))
    return normalized


def _policy(
    roots: Iterable[str | Path],
    include_globs: Iterable[str],
    ignore_globs: Iterable[str],
    suffixes: Iterable[str],
) -> RepositoryCodePolicy:
    normalized_roots = _normalized_values(roots, label="roots", transform=_normalized_root)
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
        if normalized != value or "\\" in value or PurePosixPath(value).is_absolute():
            raise ValueError("repository code glob must be normalized relative POSIX text")
        if any(part == ".." for part in PurePosixPath(value).parts):
            raise ValueError("repository code glob must not traverse parents")
        return value

    def suffix(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("repository code suffix must be bounded non-empty text")
        normalized = unicodedata.normalize("NFC", value).casefold()
        if not normalized.startswith(".") or "/" in normalized or "\\" in normalized:
            raise ValueError("repository code suffix must begin with a dot")
        return normalized

    return RepositoryCodePolicy(
        roots=normalized_roots,
        include_globs=_normalized_values(include_globs, label="include_globs", transform=pattern),
        ignore_globs=_normalized_values(ignore_globs, label="ignore_globs", transform=pattern),
        suffixes=_normalized_values(suffixes, label="suffixes", transform=suffix),
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
    identity_matches = (
        not left.st_dev
        or not left.st_ino
        or not right.st_dev
        or not right.st_ino
        or (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )
    return identity_matches and (
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _open_read(path: Path) -> int:
    # The catalog's Windows implementation uses OPEN_REPARSE_POINT; POSIX uses O_NOFOLLOW.
    from generation_catalog import _open_read_descriptor

    return _open_read_descriptor(path)


def _read_candidate(
    path: Path,
    expected: os.stat_result,
    limits: RepositoryCodeLimits,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    if expected.st_size > limits.max_file_bytes:
        raise ValueError("repository code file byte limit exceeded")
    descriptor = _open_read(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file(expected, before):
            raise PermissionError("repository code file changed before no-follow open")
        content = bytearray()
        while True:
            _check_stop(deadline, cancelled)
            chunk = os.read(descriptor, limits.chunk_bytes)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limits.max_file_bytes:
                raise ValueError("repository code file byte limit exceeded")
        after = os.fstat(descriptor)
        if not _same_file(before, after) or len(content) != before.st_size:
            raise RuntimeError("repository code file changed during capture")
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _is_unsafe(current) or not _same_file(after, current):
        raise RuntimeError("repository code file changed during capture")
    return bytes(content)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


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
            {"relative_path": path, **{name: getattr(metadata, name) for name in metadata.__dataclass_fields__}}
            for path, metadata in contract.files
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
    file_keys = {"relative_path", *FileStatMetadata.__dataclass_fields__}
    for item in files_value:
        if not isinstance(item, dict) or set(item) != file_keys:
            raise ValueError("code_capture files must be closed objects")
        path = relative_path(item["relative_path"])
        metadata_values = {name: item[name] for name in FileStatMetadata.__dataclass_fields__}
        if any(isinstance(field, bool) or not isinstance(field, int) or field < 0 for field in metadata_values.values()):
            raise ValueError("code_capture file metadata must contain non-negative integers")
        metadata = FileStatMetadata(**metadata_values)
        if metadata.size > limits.max_file_bytes:
            raise ValueError("code_capture file size exceeds its ceiling")
        total_bytes += metadata.size
        if total_bytes > limits.max_total_bytes:
            raise ValueError("code_capture file bytes exceed their ceiling")
        files.append((path, metadata))
    if [path for path, _metadata in files] != sorted(path for path, _metadata in files):
        raise ValueError("code_capture files must use deterministic ordering")
    folded_files = [unicodedata.normalize("NFC", path).casefold() for path, _ in files]
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
    captured_paths = [path for path, _metadata in files] + [
        item.relative_path for item in directories
    ]
    if any(
        not any(path == root or path.startswith(root + "/") for root in policy.roots)
        for path in captured_paths
    ):
        raise ValueError("code_capture paths must remain under declared roots")
    membership = _canonical_hash(
        [
            {
                "relative_path": item.relative_path,
                "entry_count": item.entry_count,
                "entries_sha256": item.entries_sha256,
            }
            for item in directories
        ]
    )
    if value["membership_sha256"] != membership:
        raise ValueError("code_capture membership_sha256 is not canonical")
    contract = CodeCaptureContract(policy, limits, tuple(files), tuple(directories), membership)
    normalized = code_capture_as_dict(contract)
    if value != normalized:
        raise ValueError("code_capture must use canonical values")
    return normalized


def collect_repository_code(
    checkout_root: Path,
    roots: Iterable[str | Path],
    include_globs: Iterable[str],
    ignore_globs: Iterable[str],
    suffixes: Iterable[str],
    limits: RepositoryCodeLimits = RepositoryCodeLimits(),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CorpusSnapshot:
    """Capture exact repository bytes and a complete bounded membership contract."""
    root = Path(checkout_root).resolve(strict=True)
    resolve_repository_scope(root)
    selected_policy = _policy(roots, include_globs, ignore_globs, suffixes)
    if not isinstance(limits, RepositoryCodeLimits):
        raise TypeError("limits must be RepositoryCodeLimits")
    root_info = root.lstat()
    if _is_unsafe(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise PermissionError("checkout root must be a regular directory")

    captured: list[CapturedSource] = []
    file_stats: list[tuple[str, FileStatMetadata]] = []
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

    def walk(directory: Path, depth: int) -> None:
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
        with os.scandir(directory) as iterator:
            for entry in iterator:
                _check_stop(deadline, cancelled)
                entries_seen += 1
                if entries_seen > limits.max_entries:
                    raise ValueError("repository code entry limit exceeded")
                info = entry.stat(follow_symlinks=False)
                raw_entries.append((entry.name, Path(entry.path), info))
        after_scan = directory.lstat()
        if not _same_file(before, after_scan):
            raise RuntimeError("repository code directory changed during capture")
        membership_entries = []
        normalized_names: dict[str, str] = {}
        for name, _path, info in raw_entries:
            normalized_name = unicodedata.normalize("NFC", name)
            key = normalized_name.casefold()
            if key in normalized_names and normalized_names[key] != name:
                raise ValueError("repository directory contains a normalization collision")
            normalized_names[key] = name
            membership_entries.append({"name": normalized_name, "kind": _kind(info)})
        membership_entries.sort(key=lambda item: (item["name"], item["kind"]))
        relative_directory = canonical_relative(directory)
        directories.append(
            DirectoryMembership(
                relative_path=relative_directory,
                entry_count=len(membership_entries),
                entries_sha256=_canonical_hash(membership_entries),
            )
        )
        for name, path, info in sorted(raw_entries, key=lambda item: unicodedata.normalize("NFC", item[0])):
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
                    walk(path, depth + 1)
                continue
            if ignored_directory or policy_ignored:
                continue
            if selected_policy.suffixes and path.suffix.casefold() not in selected_policy.suffixes:
                continue
            if selected_policy.include_globs and not _matches(relative, selected_policy.include_globs):
                continue
            if len(captured) >= limits.max_files:
                raise ValueError("repository code file limit exceeded")
            content = _read_candidate(
                path, info, limits, deadline=deadline, cancelled=cancelled
            )
            content.decode("utf-8", errors="strict")
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise ValueError("repository code total byte limit exceeded")
            digest = hashlib.sha256(content).hexdigest()
            record = SourceRecord(
                logical_id=f"source:{relative}",
                relative_path=relative,
                sha256=digest,
                size=len(content),
                media_type="text/x-python" if path.suffix.casefold() == ".py" else "text/plain",
                language=language_for_path(path),
                git_oid=None,
            )
            captured.append(CapturedSource(record, SourceMetadata(type="code"), content))
            file_stats.append((relative, _stat_metadata(info)))

    for relative_root in selected_policy.roots:
        candidate = root.joinpath(*PurePosixPath(relative_root).parts)
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PermissionError("repository code root escapes checkout") from exc
        info = candidate.lstat()
        if _is_unsafe(info):
            raise PermissionError("repository code root is a link or reparse point")
        if stat.S_ISDIR(info.st_mode):
            walk(candidate, 0)
        elif stat.S_ISREG(info.st_mode):
            raise ValueError("repository code roots must be directories")
        else:
            raise PermissionError("repository code root is not a regular directory")

    for expected in directories:
        _check_stop(deadline, cancelled)
        directory = root.joinpath(*PurePosixPath(expected.relative_path).parts)
        try:
            info = directory.lstat()
            if _is_unsafe(info) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("repository code membership changed during capture")
            current_entries = []
            current_names: dict[str, str] = {}
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    _check_stop(deadline, cancelled)
                    entry_info = entry.stat(follow_symlinks=False)
                    name = unicodedata.normalize("NFC", entry.name)
                    key = name.casefold()
                    if key in current_names and current_names[key] != entry.name:
                        raise RuntimeError("repository code membership changed by collision")
                    current_names[key] = entry.name
                    current_entries.append({"name": name, "kind": _kind(entry_info)})
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
        path = root.joinpath(*PurePosixPath(source.record.relative_path).parts)
        try:
            info = path.lstat()
            current = _read_candidate(
                path, info, limits, deadline=deadline, cancelled=cancelled
            )
        except OSError as exc:
            raise RuntimeError("repository code source changed during capture") from exc
        if hashlib.sha256(current).hexdigest() != source.record.sha256:
            raise RuntimeError("repository code source changed during capture")

    captured.sort(key=lambda source: source.record.relative_path)
    file_stats.sort(key=lambda item: item[0])
    directories.sort(key=lambda item: item.relative_path)
    membership_hash = _canonical_hash(
        [
            {
                "relative_path": item.relative_path,
                "entry_count": item.entry_count,
                "entries_sha256": item.entries_sha256,
            }
            for item in directories
        ]
    )
    contract = CodeCaptureContract(
        selected_policy,
        limits,
        tuple(file_stats),
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


def seal_workspace(snapshot: CorpusSnapshot, root: Path) -> SealedWorkspace:
    """Write captured bytes to a new owner-only tree and request read-only files."""
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("seal_workspace requires a repository CorpusSnapshot")
    destination = Path(root)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    owner_only = os.name == "posix"
    entries = []
    try:
        for source in snapshot.sources:
            relative = PurePosixPath(source.record.relative_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("captured source path is unsafe")
            parent = destination
            for part in relative.parts[:-1]:
                parent /= part
                if not parent.exists():
                    parent.mkdir(mode=0o700)
                    fsync_directory(parent.parent)
                info = parent.lstat()
                if _is_unsafe(info) or not stat.S_ISDIR(info.st_mode):
                    raise PermissionError("sealed workspace parent is unsafe")
            target = parent / relative.name
            descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            try:
                written = 0
                while written < len(source.content):
                    written += os.write(descriptor, source.content[written : written + 64 * 1024])
                os.fsync(descriptor)
                try:
                    os.fchmod(descriptor, 0o400)
                except (AttributeError, OSError):
                    os.chmod(target, stat.S_IREAD)
            finally:
                os.close(descriptor)
            fsync_directory(parent)
            entries.append((source.record.relative_path, source.record.size, source.record.sha256))
        fsync_directory(destination)
    except BaseException:
        # Preserve partial state for forensic inspection; callers choose cleanup.
        raise
    workspace = SealedWorkspace(
        destination.resolve(strict=True),
        snapshot.corpus_sha256,
        tuple(entries),
        owner_only,
        True,
    )
    verify_workspace_seal(workspace, snapshot)
    return workspace


def verify_workspace_seal(workspace: SealedWorkspace, snapshot: CorpusSnapshot) -> None:
    """Reopen every sealed file no-follow and reject any membership or byte change."""
    if not isinstance(workspace, SealedWorkspace):
        raise TypeError("workspace must be SealedWorkspace")
    if not isinstance(snapshot, CorpusSnapshot) or snapshot.code_capture is None:
        raise TypeError("snapshot must be a repository CorpusSnapshot")
    expected = tuple(
        (source.record.relative_path, source.record.size, source.record.sha256)
        for source in snapshot.sources
    )
    if workspace.source_manifest_sha256 != snapshot.corpus_sha256 or workspace.entries != expected:
        raise WorkspaceChanged("sealed workspace source manifest changed")
    actual_paths = []
    pending = [workspace.root]
    visited = 0
    while pending:
        directory = pending.pop()
        info = directory.lstat()
        if _is_unsafe(info) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceChanged("sealed workspace directory changed or became a link/reparse point")
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        visited += len(entries)
        if visited > snapshot.code_capture.limits.max_entries:
            raise WorkspaceChanged("sealed workspace entry range exceeded")
        for entry in entries:
            path = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            if _is_unsafe(metadata):
                raise WorkspaceChanged("sealed workspace member became a link or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                actual_paths.append(path.relative_to(workspace.root).as_posix())
            else:
                raise WorkspaceChanged("sealed workspace contains a device")
    if tuple(sorted(actual_paths)) != tuple(item[0] for item in expected):
        raise WorkspaceChanged("sealed workspace has extra or missing files")
    for relative, size, digest in expected:
        path = workspace.root.joinpath(*PurePosixPath(relative).parts)
        try:
            descriptor = _open_read(path)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                    raise WorkspaceChanged("sealed workspace file size changed")
                hasher = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, snapshot.code_capture.limits.chunk_bytes)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise WorkspaceChanged("sealed workspace file exceeded captured range")
                    hasher.update(chunk)
                after = os.fstat(descriptor)
                if not _same_file(before, after) or total != size or hasher.hexdigest() != digest:
                    raise WorkspaceChanged("sealed workspace file content changed")
            finally:
                os.close(descriptor)
            current = path.lstat()
            if _is_unsafe(current) or not _same_file(after, current):
                raise WorkspaceChanged("sealed workspace file identity changed")
        except WorkspaceChanged:
            raise
        except (OSError, PermissionError) as exc:
            raise WorkspaceChanged("sealed workspace file cannot be verified") from exc


__all__ = [
    "CodeCaptureContract",
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
]
