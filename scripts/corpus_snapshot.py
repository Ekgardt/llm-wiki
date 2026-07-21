"""Coherent, bounded corpus snapshots built from exact authoritative bytes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as datetime_time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml
from bounded_io import read_stable_bytes
from code_languages import language_for_path
from vault_editorial import EDITORIAL_NAMES

COLLECTOR_VERSION = "corpus-collector/v1"
EXTRACTOR_VERSION = "markdown-heading-extractor/v2"

MAX_CORPUS_FILES = 10_000
MAX_CORPUS_FILE_BYTES = 8 * 1024 * 1024
MAX_CORPUS_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CORPUS_INSPECTED_ENTRIES = 50_000
MAX_CORPUS_DIRECTORIES = 5_000
MAX_CORPUS_DEPTH = 16
MAX_CORPUS_HEADINGS = 100_000
MAX_CORPUS_CHUNKS = 100_000
DEFAULT_DEADLINE_SECONDS = 30.0

PROJECT_FILES = frozenset({"state.md", "journal.md", "context.md"})
APPROVED_CODE_ROOTS = frozenset(
    {"benchmark", "docs", "integrations", "rules", "scripts", "skills", "tests"}
)
ARCHIVE_DIRECTORIES = frozenset({"archive", ".archive", "_archive"})
SKIP_DIRECTORIES = frozenset({"_template", "gaps", "raw-sources"})
_HEADING = re.compile(
    rb"(?m)^[ \t]{0,3}(#{1,6})(?:[ \t]+([^\r\n]*?)|[ \t]*)(?:\r?\n|$)"
)
_FENCE = re.compile(rb"^[ ]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+[ \t]*$")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]")


class CorpusChanged(RuntimeError):
    """Live corpus membership or content differs from a captured snapshot."""


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    type: str
    project: str | None = None
    authority: str | None = None
    confidence: str | None = None
    status: str = "active"
    valid_from: str | None = None
    valid_to: str | None = None
    language: str | None = None

    @property
    def source_authority(self) -> str | None:
        return self.authority


@dataclass(frozen=True, slots=True)
class SourceRecord:
    logical_id: str
    relative_path: str
    sha256: str
    size: int
    media_type: str
    language: str | None
    git_oid: str | None


@dataclass(frozen=True, slots=True)
class CapturedSource:
    record: SourceRecord
    metadata: SourceMetadata
    content: bytes

    @property
    def captured_bytes(self) -> bytes:
        return self.content


@dataclass(frozen=True, slots=True)
class RetrievalChunk:
    id: str
    source_id: str
    source_path: str
    parent_page: str
    heading_ancestry: tuple[str, ...]
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    text: str
    source_sha256: str
    span_sha256: str
    type: str
    project: str | None
    authority: str | None
    confidence: str | None
    status: str
    valid_from: str | None
    valid_to: str | None
    language: str | None

    @property
    def chunk_id(self) -> str:
        return self.id

    @property
    def source_hash(self) -> str:
        return self.source_sha256


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    daily_paths: tuple[str, ...]
    code_roots: tuple[str, ...]
    include_historical: bool
    as_of: str | None
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_entries: int
    max_directories: int
    max_depth: int


@dataclass(frozen=True, slots=True)
class RepositoryCodeLimits:
    max_files: int = 10_000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_entries: int = 50_000
    max_directories: int = 5_000
    max_depth: int = 32
    chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        maxima = {
            "max_files": 1_000_000,
            "max_file_bytes": 1024**3,
            "max_total_bytes": 16 * 1024**3,
            "max_entries": 5_000_000,
            "max_directories": 1_000_000,
            "max_depth": 256,
            "chunk_bytes": 8 * 1024 * 1024,
        }
        minima = {"max_depth": 1, "chunk_bytes": 4096}
        for field, maximum in maxima.items():
            value = getattr(self, field)
            minimum = minima.get(field, 1)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")


@dataclass(frozen=True, slots=True)
class RepositoryCodePolicy:
    roots: tuple[str, ...]
    include_globs: tuple[str, ...]
    ignore_globs: tuple[str, ...]
    suffixes: tuple[str, ...]

    def __post_init__(self) -> None:
        bounds = {
            "roots": (1, 128),
            "include_globs": (1, 256),
            "ignore_globs": (0, 256),
            "suffixes": (1, 128),
        }
        for name, (minimum, maximum) in bounds.items():
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or not minimum <= len(values) <= maximum
                or values != tuple(sorted(set(values)))
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(f"{name} must be a bounded sorted unique tuple")
        for root in self.roots:
            pure = PurePosixPath(root)
            if (
                len(root) > 4096
                or root != unicodedata.normalize("NFC", root)
                or "\\" in root
                or pure.is_absolute()
                or root == "."
                or any(part in {"", ".", ".."} for part in pure.parts)
            ):
                raise ValueError("roots must contain normalized relative POSIX paths")
        for name in ("include_globs", "ignore_globs"):
            if any(
                len(value) > 4096
                or "\\" in value
                or PurePosixPath(value).is_absolute()
                or PureWindowsPath(value).drive
                or PureWindowsPath(value).root
                or ".." in PurePosixPath(value).parts
                or value.startswith("./")
                or "//" in value
                or any(part == "." for part in value.split("/"))
                for value in getattr(self, name)
            ):
                raise ValueError(f"{name} must contain normalized relative POSIX globs")
        if any(
            len(value) > 128
            or value != value.casefold()
            or not value.startswith(".")
            or "/" in value
            or "\\" in value
            for value in self.suffixes
        ):
            raise ValueError("suffixes must contain normalized lowercase suffixes")


@dataclass(frozen=True, slots=True)
class FileStatMetadata:
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    device: int
    inode: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CodeCaptureFile:
    source_id: str
    relative_path: str
    sha256: str
    stat: FileStatMetadata

    def __post_init__(self) -> None:
        try:
            encoded_source_id = self.source_id.encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise ValueError("source_id must be valid UTF-8 text") from exc
        if (
            not isinstance(self.source_id, str)
            or not self.source_id
            or not encoded_source_id
            or len(self.source_id) > 512
            or self.source_id != unicodedata.normalize("NFC", self.source_id)
        ):
            raise ValueError("source_id must be normalized UTF-8 text of at most 512 characters")
        pure = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or len(self.relative_path) > 4096
            or self.relative_path != unicodedata.normalize("NFC", self.relative_path)
            or "\\" in self.relative_path
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("relative_path must be normalized relative POSIX text")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("sha256 must be lowercase SHA-256")
        if not isinstance(self.stat, FileStatMetadata):
            raise TypeError("stat must be FileStatMetadata")


@dataclass(frozen=True, slots=True)
class DirectoryMembership:
    relative_path: str
    entry_count: int
    entries_sha256: str

    def __post_init__(self) -> None:
        pure = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or len(self.relative_path) > 4096
            or self.relative_path != unicodedata.normalize("NFC", self.relative_path)
            or "\\" in self.relative_path
            or pure.is_absolute()
            or self.relative_path == "."
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("relative_path must be normalized relative POSIX text")
        if (
            isinstance(self.entry_count, bool)
            or not isinstance(self.entry_count, int)
            or self.entry_count < 0
        ):
            raise ValueError("entry_count must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", self.entries_sha256) is None:
            raise ValueError("entries_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CodeCaptureContract:
    policy: RepositoryCodePolicy
    limits: RepositoryCodeLimits
    files: tuple[CodeCaptureFile, ...]
    directories: tuple[DirectoryMembership, ...]
    membership_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy, RepositoryCodePolicy):
            raise TypeError("policy must be RepositoryCodePolicy")
        if not isinstance(self.limits, RepositoryCodeLimits):
            raise TypeError("limits must be RepositoryCodeLimits")
        if (
            not isinstance(self.files, tuple)
            or any(not isinstance(item, CodeCaptureFile) for item in self.files)
            or tuple(item.relative_path for item in self.files)
            != tuple(sorted(item.relative_path for item in self.files))
        ):
            raise ValueError("files must be an ordered CodeCaptureFile tuple")
        file_paths = [item.relative_path.casefold() for item in self.files]
        source_ids = [item.source_id for item in self.files]
        if len(file_paths) != len(set(file_paths)) or len(source_ids) != len(set(source_ids)):
            raise ValueError("files contain a path or source ID collision")
        if (
            not isinstance(self.directories, tuple)
            or any(not isinstance(item, DirectoryMembership) for item in self.directories)
            or tuple(item.relative_path for item in self.directories)
            != tuple(sorted(item.relative_path for item in self.directories))
        ):
            raise ValueError("directories must be an ordered DirectoryMembership tuple")
        if re.fullmatch(r"[0-9a-f]{64}", self.membership_sha256) is None:
            raise ValueError("membership_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    sources: tuple[CapturedSource, ...]
    chunks: tuple[RetrievalChunk, ...]
    corpus_sha256: str
    policy: SnapshotPolicy
    collector_version: str = COLLECTOR_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    code_capture: CodeCaptureContract | None = None

    @property
    def source_hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (source.record.relative_path, source.record.sha256)
            for source in self.sources
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    relative: str
    kind: str
    project: str | None
    seal: tuple[_PathIdentity, ...]
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    mode: int
    size: int
    ctime_ns: int
    attributes: int


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(encoded)


def canonical_chunk_id(
    *,
    source_id: str,
    source_path: str,
    byte_start: int,
    byte_end: int,
    span_sha256: str,
    extractor_version: str = EXTRACTOR_VERSION,
) -> str:
    """Return the shared content-bound identity for one retrieval chunk."""
    return _canonical_hash(
        {
            "extractor": extractor_version,
            "parent": source_id,
            "path": source_path,
            "range": [byte_start, byte_end],
            "sha256": span_sha256,
        }
    )


def _manifest_source(record: SourceRecord | Mapping[str, object]) -> dict[str, str]:
    if isinstance(record, SourceRecord):
        logical_id = record.logical_id
        relative_path = record.relative_path
        digest = record.sha256
    elif isinstance(record, Mapping):
        if set(record) != {"logical_id", "relative_path", "sha256"}:
            raise ValueError("canonical source manifest entries must be closed objects")
        logical_id = record["logical_id"]
        relative_path = record["relative_path"]
        digest = record["sha256"]
    else:
        raise TypeError("canonical source manifest entries must be SourceRecord values or objects")
    if not isinstance(logical_id, str) or not logical_id or len(logical_id) > 4096:
        raise ValueError("canonical source logical_id must be a bounded non-empty string")
    if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 4096:
        raise ValueError("canonical source relative_path must be a bounded non-empty string")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("canonical source sha256 must be a lowercase SHA-256 digest")
    return {"relative_path": relative_path, "sha256": digest, "logical_id": logical_id}


def _manifest_policy(policy: SnapshotPolicy | Mapping[str, object]) -> dict[str, object]:
    if isinstance(policy, SnapshotPolicy):
        daily_paths = policy.daily_paths
        code_roots = policy.code_roots
        include_historical = policy.include_historical
        as_of = policy.as_of
    elif isinstance(policy, Mapping):
        if set(policy) != {"daily_paths", "code_roots", "include_historical", "as_of"}:
            raise ValueError("canonical source manifest policy must be a closed object")
        daily_paths = policy["daily_paths"]
        code_roots = policy["code_roots"]
        include_historical = policy["include_historical"]
        as_of = policy["as_of"]
    else:
        raise TypeError("canonical source manifest policy must be SnapshotPolicy or an object")
    if not isinstance(daily_paths, (list, tuple)) or not all(
        isinstance(value, str) for value in daily_paths
    ):
        raise ValueError("canonical daily_paths must be an array of strings")
    if not isinstance(code_roots, (list, tuple)) or not all(
        isinstance(value, str) for value in code_roots
    ):
        raise ValueError("canonical code_roots must be an array of strings")
    if not isinstance(include_historical, bool):
        raise ValueError("canonical include_historical must be boolean")
    if as_of is not None and not isinstance(as_of, str):
        raise ValueError("canonical as_of must be null or a string")
    return {
        "daily_paths": list(daily_paths),
        "code_roots": list(code_roots),
        "include_historical": include_historical,
        "as_of": as_of,
    }


def canonical_source_manifest(
    sources: Iterable[SourceRecord | Mapping[str, object]],
    policy: SnapshotPolicy | Mapping[str, object],
    *,
    collector_version: str = COLLECTOR_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
) -> dict[str, object]:
    """Return the one canonical source manifest shared by every generation consumer."""
    if not isinstance(collector_version, str) or not collector_version:
        raise ValueError("collector_version must be a non-empty string")
    if not isinstance(extractor_version, str) or not extractor_version:
        raise ValueError("extractor_version must be a non-empty string")
    entries = sorted(
        (_manifest_source(source) for source in sources),
        key=lambda item: (item["relative_path"], item["logical_id"]),
    )
    paths = [entry["relative_path"] for entry in entries]
    logical_ids = [entry["logical_id"] for entry in entries]
    if len(paths) != len(set(paths)) or len(logical_ids) != len(set(logical_ids)):
        raise ValueError("canonical source membership paths and logical IDs must be unique")
    return {
        "collector": collector_version,
        "extractor": extractor_version,
        "policy": _manifest_policy(policy),
        "sources": entries,
    }


def canonical_source_manifest_sha256(
    sources: Iterable[SourceRecord | Mapping[str, object]],
    policy: SnapshotPolicy | Mapping[str, object],
    *,
    collector_version: str = COLLECTOR_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
) -> str:
    """Hash the shared canonical source manifest."""
    return _canonical_hash(
        canonical_source_manifest(
            sources,
            policy,
            collector_version=collector_version,
            extractor_version=extractor_version,
        )
    )


def validate_canonical_source_manifest(value: object) -> dict[str, object]:
    """Validate and normalize one closed shared canonical source manifest."""
    if not isinstance(value, Mapping) or set(value) != {
        "collector",
        "extractor",
        "policy",
        "sources",
    }:
        raise ValueError("canonical source manifest must be a closed object")
    sources = value["sources"]
    if not isinstance(sources, list):
        raise ValueError("canonical source manifest sources must be an array")
    normalized = canonical_source_manifest(
        sources,
        value["policy"],
        collector_version=value["collector"],
        extractor_version=value["extractor"],
    )
    if value != normalized:
        raise ValueError("canonical source manifest must use deterministic source ordering")
    return normalized


def _positive_limit(value: object, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _deadline_value(deadline: float | None, deadline_seconds: float | None) -> float:
    if deadline is not None and deadline_seconds is not None:
        raise ValueError("deadline and deadline_seconds are mutually exclusive")
    if deadline is not None:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise ValueError("deadline must be a monotonic timestamp")
        return float(deadline)
    seconds = DEFAULT_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        raise ValueError("deadline_seconds must be non-negative")
    return time.monotonic() + float(seconds)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("corpus collection deadline reached")


def _check_processing_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    if bool(cancelled and cancelled()):
        raise TimeoutError("corpus collection cancelled")
    if deadline is not None:
        _check_deadline(deadline)


def _relative_posix(value: str | Path, *, prefixes: tuple[str, ...]) -> str:
    raw = str(value)
    if not raw or "\\" in raw:
        raise ValueError("source path must be a normalized relative POSIX path")
    pure = PurePosixPath(raw)
    normalized = unicodedata.normalize("NFC", pure.as_posix())
    if (
        pure.is_absolute()
        or normalized != raw
        or any(part in {"", ".", ".."} for part in pure.parts)
        or not any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in prefixes)
    ):
        raise ValueError("source path must be a normalized relative POSIX path")
    return normalized


def _code_root(value: str | Path) -> str:
    raw = str(value)
    windows_path = PureWindowsPath(raw)
    if "\\" in raw or ":" in raw or windows_path.drive or windows_path.root:
        raise ValueError("code root must be a normalized approved relative POSIX path")
    try:
        return _relative_posix(value, prefixes=tuple(sorted(APPROVED_CODE_ROOTS)))
    except ValueError as exc:
        raise ValueError(
            "code root must be a normalized approved relative POSIX path"
        ) from exc


def _safe_info(path: Path) -> os.stat_result:
    info = path.lstat()
    if path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise PermissionError(f"unsafe corpus path: {path}")
    return info


def _identity(path: Path, info: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        path=path,
        device=info.st_dev,
        inode=info.st_ino,
        mode=info.st_mode,
        size=info.st_size,
        ctime_ns=info.st_ctime_ns,
        attributes=getattr(info, "st_file_attributes", 0) or 0,
    )


def _seal_path(
    vault: Path,
    path: Path,
    *,
    target_directory: bool,
    max_components: int,
) -> tuple[_PathIdentity, ...]:
    seal = _build_path_seal(
        vault,
        path,
        target_directory=target_directory,
        max_components=max_components,
    )
    _verify_seal(seal, changed_error=PermissionError)
    return seal


def _build_path_seal(
    vault: Path,
    path: Path,
    *,
    target_directory: bool,
    max_components: int,
) -> tuple[_PathIdentity, ...]:
    try:
        relative = path.relative_to(vault)
    except ValueError as exc:
        raise PermissionError("corpus path escapes the resolved vault") from exc
    if len(relative.parts) > max_components:
        raise ValueError("corpus ancestor depth limit exceeded")
    components = [vault]
    current = vault
    for part in relative.parts:
        current /= part
        components.append(current)
    seal = []
    for index, component in enumerate(components):
        info = _safe_info(component)
        is_target = index == len(components) - 1
        expected_directory = not is_target or target_directory
        if expected_directory != stat.S_ISDIR(info.st_mode):
            expected = "directory" if expected_directory else "regular file"
            raise PermissionError(f"corpus path component must be a {expected}: {component}")
        if is_target and not target_directory and not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"corpus source must be a regular file: {component}")
        try:
            component.resolve(strict=True).relative_to(vault)
        except ValueError as exc:
            raise PermissionError("corpus path resolves outside the vault") from exc
        seal.append(_identity(component, info))
    return tuple(seal)


def _open_sealed_posix_path(
    vault: Path,
    path: Path,
    *,
    target_directory: bool,
    max_components: int,
) -> tuple[tuple[_PathIdentity, ...], int]:
    seal = _build_path_seal(
        vault,
        path,
        target_directory=target_directory,
        max_components=max_components,
    )
    descriptor = _open_descriptor_chain(seal, changed_error=PermissionError)
    return seal, descriptor


def _verify_seal(
    seal: tuple[_PathIdentity, ...], *, changed_error: type[Exception] = CorpusChanged
) -> None:
    for expected in seal:
        try:
            current = _identity(expected.path, _safe_info(expected.path))
        except (OSError, PermissionError) as exc:
            raise changed_error(f"corpus ancestor changed: {expected.path}") from exc
        if current != expected:
            raise changed_error(f"corpus ancestor changed: {expected.path}")
    _verify_descriptor_chain(seal, changed_error=changed_error)


def _verify_descriptor_chain(
    seal: tuple[_PathIdentity, ...], *, changed_error: type[Exception]
) -> None:
    if os.name != "posix" or not seal:
        return
    descriptor = _open_descriptor_chain(seal, changed_error=changed_error)
    os.close(descriptor)


def _open_descriptor_chain(
    seal: tuple[_PathIdentity, ...], *, changed_error: type[Exception]
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(seal[0].path, flags | getattr(os, "O_DIRECTORY", 0))
        if _identity(seal[0].path, os.fstat(descriptor)) != seal[0]:
            raise changed_error(f"corpus ancestor changed: {seal[0].path}")
        for expected in seal[1:]:
            child_flags = flags
            if stat.S_ISDIR(expected.mode):
                child_flags |= getattr(os, "O_DIRECTORY", 0)
            child = os.open(expected.path.name, child_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if _identity(expected.path, os.fstat(descriptor)) != expected:
                raise changed_error(f"corpus ancestor changed: {expected.path}")
        result = descriptor
        descriptor = -1
        return result
    except OSError as exc:
        raise changed_error("corpus ancestor changed during descriptor traversal") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_opened_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _same_descriptor_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _descriptor_flags(*, directory: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _read_bounded_descriptor(descriptor: int, max_bytes: int) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise PermissionError("corpus source descriptor must be a regular file")
    if before.st_size > max_bytes:
        raise ValueError(f"corpus source exceeds {max_bytes} bytes")
    chunks = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ValueError(f"corpus source exceeds {max_bytes} bytes")
    after = os.fstat(descriptor)
    if not _same_opened_object(before, after):
        raise CorpusChanged("corpus source changed during descriptor read")
    return content


class _Discovery:
    def __init__(
        self,
        vault: Path,
        *,
        max_files: int,
        max_entries: int,
        max_directories: int,
        max_depth: int,
        max_file_bytes: int,
        max_total_bytes: int,
        deadline: float,
        include_archives: bool,
    ) -> None:
        self.vault = vault
        self.max_files = max_files
        self.max_entries = max_entries
        self.max_directories = max_directories
        self.max_depth = max_depth
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.deadline = deadline
        self.include_archives = include_archives
        self.entries = 0
        self.directories = 0
        self.total_bytes = 0
        self.candidates: dict[str, _Candidate] = {}

    def add(self, path: Path, kind: str, project: str | None = None) -> None:
        _check_deadline(self.deadline)
        if os.name == "posix":
            _, descriptor = _open_sealed_posix_path(
                self.vault,
                path,
                target_directory=False,
                max_components=self.max_depth + 3,
            )
            try:
                content = _read_bounded_descriptor(descriptor, self.max_file_bytes)
            finally:
                os.close(descriptor)
            self._store(path, kind, project, (), content)
            return
        seal = _seal_path(
            self.vault,
            path,
            target_directory=False,
            max_components=self.max_depth + 3,
        )
        self._store(path, kind, project, seal, None)

    def _store(
        self,
        path: Path,
        kind: str,
        project: str | None,
        seal: tuple[_PathIdentity, ...],
        content: bytes | None,
    ) -> None:
        relative = unicodedata.normalize("NFC", path.relative_to(self.vault).as_posix())
        if relative in self.candidates:
            raise ValueError(f"duplicate corpus source path: {relative}")
        if content is not None:
            self.total_bytes += len(content)
            if self.total_bytes > self.max_total_bytes:
                raise ValueError("corpus total byte limit exceeded")
        candidate = _Candidate(path, relative, kind, project, seal, content)
        self.candidates[relative] = candidate
        if len(self.candidates) > self.max_files:
            raise ValueError("corpus file limit exceeded")

    def walk(self, root: Path, kind: str) -> None:
        if not root.exists():
            return
        if os.name == "posix":
            self._walk_posix(root, kind)
            return
        root_seal = _seal_path(
            self.vault,
            root,
            target_directory=True,
            max_components=self.max_depth + 3,
        )
        stack = [(root, 0, root_seal)]
        while stack:
            current, depth, current_seal = stack.pop()
            _check_deadline(self.deadline)
            _verify_seal(current_seal)
            self.directories += 1
            if self.directories > self.max_directories:
                raise ValueError("corpus directory limit exceeded")
            if depth > self.max_depth:
                raise ValueError("corpus depth limit exceeded")
            children: list[tuple[Path, tuple[_PathIdentity, ...]]] = []
            entries = []
            with os.scandir(current) as iterator:
                iterator = iter(iterator)
                while True:
                    _check_deadline(self.deadline)
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    self.entries += 1
                    if self.entries > self.max_entries:
                        raise ValueError("corpus traversal entry limit exceeded")
                    entries.append(entry)
            _verify_seal(current_seal)
            for entry in sorted(entries, key=lambda item: item.name):
                _check_deadline(self.deadline)
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                unsafe = entry.is_symlink() or bool(
                    (getattr(info, "st_file_attributes", 0) or 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                if unsafe:
                    raise PermissionError(f"unsafe corpus path: {path}")
                if stat.S_ISDIR(info.st_mode):
                    is_archive = entry.name in ARCHIVE_DIRECTORIES
                    excluded = (
                        entry.name in SKIP_DIRECTORIES
                        or (is_archive and not self.include_archives)
                        or (entry.name.startswith(".") and not is_archive)
                    )
                    if not excluded:
                        if depth >= self.max_depth:
                            raise ValueError("corpus depth limit exceeded")
                        children.append(
                            (
                                path,
                                _seal_path(
                                    self.vault,
                                    path,
                                    target_directory=True,
                                    max_components=self.max_depth + 3,
                                ),
                            )
                        )
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if kind == "note":
                    if path.suffix.casefold() == ".md" and path.name not in EDITORIAL_NAMES:
                        self.add(path, kind)
                elif kind == "project":
                    if path.name in PROJECT_FILES:
                        relative_project = path.relative_to(root).parts
                        project = relative_project[0] if len(relative_project) > 1 else None
                        self.add(path, kind, project)
                else:
                    self.add(path, kind)
            _verify_seal(current_seal)
            stack.extend(
                (child, depth + 1, seal) for child, seal in reversed(children)
            )

    def _walk_posix(self, root: Path, kind: str) -> None:
        _, descriptor = _open_sealed_posix_path(
            self.vault,
            root,
            target_directory=True,
            max_components=self.max_depth + 3,
        )
        try:
            self._walk_posix_directory(root, root, 0, descriptor, kind)
        finally:
            os.close(descriptor)

    def _walk_posix_directory(
        self,
        root: Path,
        current: Path,
        depth: int,
        descriptor: int,
        kind: str,
    ) -> None:
        _check_deadline(self.deadline)
        opened_directory = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_directory.st_mode):
            raise PermissionError("corpus traversal descriptor must be a directory")
        self.directories += 1
        if self.directories > self.max_directories:
            raise ValueError("corpus directory limit exceeded")
        if depth > self.max_depth:
            raise ValueError("corpus depth limit exceeded")
        entries = []
        with os.scandir(descriptor) as iterator:
            iterator = iter(iterator)
            while True:
                _check_deadline(self.deadline)
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                self.entries += 1
                if self.entries > self.max_entries:
                    raise ValueError("corpus traversal entry limit exceeded")
                entries.append(
                    (
                        entry.name,
                        os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False),
                    )
                )
        if not _same_descriptor_identity(opened_directory, os.fstat(descriptor)):
            raise CorpusChanged("corpus directory descriptor changed during traversal")

        directory_flags = _descriptor_flags(directory=True)
        file_flags = _descriptor_flags(directory=False)
        for name, info in sorted(entries, key=lambda item: item[0]):
            _check_deadline(self.deadline)
            path = current / name
            unsafe = stat.S_ISLNK(info.st_mode) or bool(
                (getattr(info, "st_file_attributes", 0) or 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if unsafe:
                raise PermissionError(f"unsafe corpus path: {path}")
            if stat.S_ISDIR(info.st_mode):
                is_archive = name in ARCHIVE_DIRECTORIES
                excluded = (
                    name in SKIP_DIRECTORIES
                    or (is_archive and not self.include_archives)
                    or (name.startswith(".") and not is_archive)
                )
                if excluded:
                    continue
                if depth >= self.max_depth:
                    raise ValueError("corpus depth limit exceeded")
                child = os.open(name, directory_flags, dir_fd=descriptor)
                try:
                    if not _same_descriptor_identity(info, os.fstat(child)):
                        raise CorpusChanged("corpus child directory changed before open")
                    self._walk_posix_directory(root, path, depth + 1, child, kind)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or not self._eligible(path, kind):
                continue
            source = os.open(name, file_flags, dir_fd=descriptor)
            try:
                if not _same_opened_object(info, os.fstat(source)):
                    raise CorpusChanged("corpus source changed before descriptor open")
                content = _read_bounded_descriptor(source, self.max_file_bytes)
            finally:
                os.close(source)
            project = None
            if kind == "project":
                relative_project = path.relative_to(root).parts
                project = relative_project[0] if len(relative_project) > 1 else None
            self._store(path, kind, project, (), content)

    @staticmethod
    def _eligible(path: Path, kind: str) -> bool:
        if kind == "note":
            return path.suffix.casefold() == ".md" and path.name not in EDITORIAL_NAMES
        if kind == "project":
            return path.name in PROJECT_FILES
        return True


def _discover(vault: Path, policy: SnapshotPolicy, deadline: float) -> tuple[_Candidate, ...]:
    discovery = _Discovery(
        vault,
        max_files=policy.max_files,
        max_entries=policy.max_entries,
        max_directories=policy.max_directories,
        max_depth=policy.max_depth,
        max_file_bytes=policy.max_file_bytes,
        max_total_bytes=policy.max_total_bytes,
        deadline=deadline,
        include_archives=policy.include_historical or policy.as_of is not None,
    )
    discovery.walk(vault / "knowledge/notes", "note")
    discovery.walk(vault / "knowledge/projects", "project")
    for relative in policy.daily_paths:
        _check_deadline(deadline)
        path = vault.joinpath(*PurePosixPath(relative).parts)
        if not path.exists():
            raise FileNotFoundError(relative)
        discovery.add(path, "daily")
    for relative in policy.code_roots:
        _check_deadline(deadline)
        path = vault.joinpath(*PurePosixPath(relative).parts)
        if not path.exists():
            raise FileNotFoundError(relative)
        info = _safe_info(path)
        if stat.S_ISDIR(info.st_mode):
            discovery.walk(path, "code")
        elif stat.S_ISREG(info.st_mode):
            discovery.add(path, "code")
        else:
            raise PermissionError(f"code root must be a regular path: {relative}")
    return tuple(discovery.candidates[key] for key in sorted(discovery.candidates))


def _line_spans(content: bytes, start: int = 0) -> Iterable[tuple[int, int]]:
    offset = start
    while offset < len(content):
        newline = content.find(b"\n", offset)
        end = len(content) if newline < 0 else newline + 1
        yield offset, end
        offset = end


def _frontmatter(
    content: bytes,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], int]:
    content.decode("utf-8", errors="strict")
    lines = iter(_line_spans(content))
    try:
        first_start, first_end = next(lines)
    except StopIteration:
        return {}, 0
    if content[first_start:first_end].strip() != b"---":
        return {}, 0
    frontmatter_start = first_end
    frontmatter_end = None
    searchable_start = None
    for line_start, line_end in lines:
        _check_processing_stop(deadline, cancelled)
        if content[line_start:line_end].strip() == b"---":
            frontmatter_end = line_start
            searchable_start = line_end
            break
    if frontmatter_end is None or searchable_start is None:
        raise ValueError("unterminated YAML frontmatter")
    frontmatter_bytes = content[frontmatter_start:frontmatter_end]
    value = yaml.safe_load(frontmatter_bytes.decode("utf-8", errors="strict"))
    if not isinstance(value, Mapping):
        raise ValueError("frontmatter must be a mapping")
    return dict(value), searchable_start


def _metadata_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise ValueError("corpus metadata values must be scalar")


def _metadata(frontmatter: Mapping[str, object], candidate: _Candidate) -> SourceMetadata:
    validity = frontmatter.get("validity", {})
    if validity is None:
        validity = {}
    if not isinstance(validity, Mapping):
        raise ValueError("validity metadata must be a mapping")
    default_type = {
        "note": "note",
        "project": "project-state" if candidate.path.name == "state.md" else "project-context",
        "daily": "daily-evidence",
        "code": "code",
    }[candidate.kind]
    status = (_metadata_value(frontmatter.get("status")) or "active").casefold()
    language = _metadata_value(frontmatter.get("language") or frontmatter.get("lang"))
    return SourceMetadata(
        type=_metadata_value(frontmatter.get("type")) or default_type,
        project=_metadata_value(frontmatter.get("project")) or candidate.project,
        authority=_metadata_value(
            frontmatter.get("source_authority", frontmatter.get("authority"))
        ),
        confidence=_metadata_value(frontmatter.get("confidence")),
        status=status,
        valid_from=_metadata_value(
            frontmatter.get("valid_from", validity.get("from"))
        ),
        valid_to=_metadata_value(frontmatter.get("valid_to", validity.get("to"))),
        language=language.casefold() if language else None,
    )


def _as_datetime(value: str | date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime_time.min)
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError:
            parsed_date = date.fromisoformat(normalized)
            result = datetime.combine(parsed_date, datetime_time.min)
    else:
        raise ValueError("as_of and validity values must be ISO dates or datetimes")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _included(metadata: SourceMetadata, policy: SnapshotPolicy) -> bool:
    if policy.as_of is None:
        return policy.include_historical or metadata.status in {"", "active"}
    instant = _as_datetime(policy.as_of)
    assert instant is not None
    start = _as_datetime(metadata.valid_from)
    end = _as_datetime(metadata.valid_to)
    return (start is None or start <= instant) and (end is None or instant < end)


def _infer_language(text: str) -> str | None:
    cyrillic = len(_CYRILLIC.findall(text))
    han = len(_HAN.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic and cyrillic >= han and cyrillic >= latin:
        return "ru"
    if han and han >= cyrillic and han >= latin:
        return "zh"
    if latin >= 3 and not cyrillic and not han:
        return "en"
    return None


def _classify_language(*, explicit: str | None, path: Path, text: str) -> str | None:
    return explicit or language_for_path(path) or _infer_language(text)


def _clean_heading(raw: bytes | None) -> str:
    title = (raw or b"").decode("utf-8", errors="strict").strip()
    return _CLOSING_HASHES.sub("", title)


def _markdown_headings(
    content: bytes,
    start: int,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[re.Match[bytes]]:
    headings: list[re.Match[bytes]] = []
    fence_character: bytes | None = None
    fence_length = 0
    for offset, end in _line_spans(content, start):
        _check_processing_stop(deadline, cancelled)
        line = content[offset:end]
        body = line.rstrip(b"\r\n")
        if fence_character is not None:
            closing = re.fullmatch(
                rb"[ ]{0,3}"
                + re.escape(fence_character)
                + rb"{" + str(fence_length).encode("ascii") + rb",}[ \t]*",
                body,
            )
            if closing:
                fence_character = None
                fence_length = 0
        else:
            opening = _FENCE.fullmatch(body)
            if opening and not (
                opening.group(1).startswith(b"`") and b"`" in opening.group(2)
            ):
                fence_character = opening.group(1)[:1]
                fence_length = len(opening.group(1))
            else:
                heading = _HEADING.match(content, offset, offset + len(line))
                if heading:
                    if len(headings) >= MAX_CORPUS_HEADINGS:
                        raise ValueError("corpus heading row ceiling exceeded")
                    headings.append(heading)
    return headings


def _retrieval_spans(
    content: bytes,
    searchable_start: int,
    *,
    heading_enabled: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    headings = (
        _markdown_headings(
            content,
            searchable_start,
            deadline=deadline,
            cancelled=cancelled,
        )
        if heading_enabled
        else []
    )
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    first_heading = headings[0].start() if headings else len(content)
    if content[searchable_start:first_heading].strip():
        spans.append((searchable_start, first_heading, ()))
    ancestry: list[tuple[int, str]] = []
    for index, heading in enumerate(headings):
        _check_processing_stop(deadline, cancelled)
        level = len(heading.group(1))
        title = _clean_heading(heading.group(2))
        ancestry = [item for item in ancestry if item[0] < level]
        ancestry.append((level, title))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        if content[heading.start():end].strip():
            if len(spans) >= MAX_CORPUS_CHUNKS:
                raise ValueError("corpus chunk row ceiling exceeded")
            spans.append((heading.start(), end, tuple(item[1] for item in ancestry)))
    return tuple(spans)


def canonical_retrieval_spans(
    source_path: str,
    content: bytes,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    """Return the shared canonical chunk ranges for captured source bytes."""
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("source_path must be a non-empty string")
    if not isinstance(content, bytes) or len(content) > MAX_CORPUS_FILE_BYTES:
        raise ValueError("captured retrieval source must be bounded bytes")
    is_markdown = PurePosixPath(source_path).suffix.casefold() == ".md"
    if is_markdown:
        _frontmatter_value, searchable_start = _frontmatter(
            content, deadline=deadline, cancelled=cancelled
        )
    else:
        content.decode("utf-8", errors="strict")
        searchable_start = 0
    return _retrieval_spans(
        content,
        searchable_start,
        heading_enabled=is_markdown,
        deadline=deadline,
        cancelled=cancelled,
    )


def _chunks(
    source: SourceRecord,
    metadata: SourceMetadata,
    content: bytes,
    searchable_start: int,
    *,
    heading_enabled: bool = True,
    extractor_version: str = EXTRACTOR_VERSION,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[RetrievalChunk, ...]:
    result = []
    spans = _retrieval_spans(
        content,
        searchable_start,
        heading_enabled=heading_enabled,
        deadline=deadline,
        cancelled=cancelled,
    )
    for start, end, heading_ancestry in spans:
        _check_processing_stop(deadline, cancelled)
        span = content[start:end]
        text = span.decode("utf-8", errors="strict")
        span_hash = _sha256(span)
        chunk_id = canonical_chunk_id(
            source_id=source.logical_id,
            source_path=source.relative_path,
            byte_start=start,
            byte_end=end,
            span_sha256=span_hash,
            extractor_version=extractor_version,
        )
        result.append(
            RetrievalChunk(
                id=chunk_id,
                source_id=source.logical_id,
                source_path=source.relative_path,
                parent_page=source.relative_path,
                heading_ancestry=heading_ancestry,
                byte_start=start,
                byte_end=end,
                line_start=content.count(b"\n", 0, start) + 1,
                line_end=content.count(b"\n", 0, end) + 1,
                text=text,
                source_sha256=source.sha256,
                span_sha256=span_hash,
                type=metadata.type,
                project=metadata.project,
                authority=metadata.authority,
                confidence=metadata.confidence,
                status=metadata.status,
                valid_from=metadata.valid_from,
                valid_to=metadata.valid_to,
                language=source.language or _infer_language(text),
            )
        )
    return tuple(result)


def canonical_retrieval_chunks(
    *,
    source_id: str,
    source_path: str,
    source_sha256: str,
    content: bytes,
    extractor_version: str = EXTRACTOR_VERSION,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[RetrievalChunk, ...]:
    """Reconstruct every canonical chunk field from authoritative source bytes."""
    path = PurePosixPath(source_path)
    if not source_id or source_id != f"source:{source_path}" or not path.parts:
        raise ValueError("source identity is not canonical")
    if _sha256(content) != source_sha256:
        raise ValueError("source bytes do not match their canonical hash")
    if path.parts[:2] == ("knowledge", "notes"):
        kind, project = "note", None
    elif path.parts[:2] == ("knowledge", "projects"):
        kind = "project"
        project = path.parts[2] if len(path.parts) > 3 else None
    elif path.parts[:2] == ("knowledge", "daily"):
        kind, project = "daily", None
    else:
        kind, project = "code", None
    is_markdown = path.suffix.casefold() == ".md"
    if is_markdown:
        frontmatter, searchable_start = _frontmatter(
            content, deadline=deadline, cancelled=cancelled
        )
    else:
        content.decode("utf-8", errors="strict")
        frontmatter, searchable_start = {}, 0
    candidate = _Candidate(Path(*path.parts), source_path, kind, project, ())
    metadata = _metadata(frontmatter, candidate)
    language = _classify_language(
        explicit=metadata.language,
        path=candidate.path,
        text=content[searchable_start:].decode("utf-8", errors="strict"),
    )
    source = SourceRecord(
        source_id,
        source_path,
        source_sha256,
        len(content),
        "text/markdown" if is_markdown else "text/plain",
        language,
        None,
    )
    return _chunks(
        source,
        metadata,
        content,
        searchable_start,
        heading_enabled=is_markdown,
        extractor_version=extractor_version,
        deadline=deadline,
        cancelled=cancelled,
    )


def _normalize_explicit_paths(
    values: Iterable[str | Path],
    normalize: Callable[[str | Path], str],
    *,
    label: str,
) -> tuple[str, ...]:
    normalized = []
    seen = set()
    for value in values:
        path = normalize(value)
        if path in seen:
            raise ValueError(f"duplicate {label}: {path}")
        seen.add(path)
        normalized.append(path)
    return tuple(sorted(normalized))


def _policy(
    *,
    daily_paths: Iterable[str | Path],
    code_roots: Iterable[str | Path],
    include_historical: bool,
    as_of: str | date | datetime | None,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    max_entries: int,
    max_directories: int,
    max_depth: int,
) -> SnapshotPolicy:
    if not isinstance(include_historical, bool):
        raise ValueError("include_historical must be boolean")
    normalized_daily = _normalize_explicit_paths(
        daily_paths,
        lambda path: _relative_posix(path, prefixes=("knowledge/daily",)),
        label="daily evidence path",
    )
    for path in normalized_daily:
        if not path.endswith(".md") or path == "knowledge/daily":
            raise ValueError("daily evidence must name an explicit Markdown file")
    normalized_code = _normalize_explicit_paths(
        code_roots, _code_root, label="code root"
    )
    for index, root in enumerate(normalized_code):
        for other in normalized_code[index + 1 :]:
            if other.startswith(root + "/"):
                raise ValueError(f"overlapping code roots are forbidden: {root}, {other}")
    as_of_value = _metadata_value(as_of)
    if as_of_value is not None:
        parsed = _as_datetime(as_of_value)
        assert parsed is not None
        as_of_value = parsed.isoformat().replace("+00:00", "Z")
    return SnapshotPolicy(
        daily_paths=normalized_daily,
        code_roots=normalized_code,
        include_historical=include_historical,
        as_of=as_of_value,
        max_files=_positive_limit(max_files, "max_files"),
        max_file_bytes=_positive_limit(max_file_bytes, "max_file_bytes"),
        max_total_bytes=_positive_limit(max_total_bytes, "max_total_bytes"),
        max_entries=_positive_limit(max_entries, "max_entries"),
        max_directories=_positive_limit(max_directories, "max_directories"),
        max_depth=_positive_limit(max_depth, "max_depth", allow_zero=True),
    )


def _capture(
    vault: Path,
    policy: SnapshotPolicy,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> CorpusSnapshot:
    candidates = _discover(vault, policy, deadline)
    captured: list[CapturedSource] = []
    chunks: list[RetrievalChunk] = []
    first_hashes: dict[str, str] = {}
    total = 0
    for candidate in candidates:
        _check_deadline(deadline)
        if candidate.content is not None:
            content = candidate.content
        else:
            _verify_seal(candidate.seal)
            content = read_stable_bytes(
                candidate.path, policy.max_file_bytes, label="corpus source"
            )
            _verify_seal(candidate.seal)
        total += len(content)
        if total > policy.max_total_bytes:
            raise ValueError("corpus total byte limit exceeded")
        is_markdown = candidate.path.suffix.casefold() == ".md"
        if is_markdown:
            frontmatter, searchable_start = _frontmatter(
                content, deadline=deadline, cancelled=cancelled
            )
        else:
            content.decode("utf-8", errors="strict")
            frontmatter, searchable_start = {}, 0
        metadata = _metadata(frontmatter, candidate)
        digest = _sha256(content)
        first_hashes[candidate.relative] = digest
        if not _included(metadata, policy):
            continue
        language = _classify_language(
            explicit=metadata.language,
            path=candidate.path,
            text=content[searchable_start:].decode("utf-8", errors="strict"),
        )
        record = SourceRecord(
            logical_id=f"source:{candidate.relative}",
            relative_path=candidate.relative,
            sha256=digest,
            size=len(content),
            media_type="text/markdown" if is_markdown else "text/plain",
            language=language,
            git_oid=None,
        )
        captured_source = CapturedSource(record, metadata, content)
        captured.append(captured_source)
        source_chunks = _chunks(
            record,
            metadata,
            content,
            searchable_start,
            heading_enabled=is_markdown,
            deadline=deadline,
            cancelled=cancelled,
        )
        if len(chunks) + len(source_chunks) > MAX_CORPUS_CHUNKS:
            raise ValueError("corpus chunk row ceiling exceeded")
        chunks.extend(source_chunks)

    _check_deadline(deadline)
    current = _discover(vault, policy, deadline)
    if tuple(candidate.relative for candidate in candidates) != tuple(
        candidate.relative for candidate in current
    ):
        raise CorpusChanged("corpus membership changed during collection")
    for candidate in current:
        _check_deadline(deadline)
        if candidate.content is not None:
            current_content = candidate.content
        else:
            _verify_seal(candidate.seal)
            current_content = read_stable_bytes(
                candidate.path, policy.max_file_bytes, label="corpus validation source"
            )
            _verify_seal(candidate.seal)
        if _sha256(current_content) != first_hashes[candidate.relative]:
            raise CorpusChanged(f"corpus source changed during collection: {candidate.relative}")

    corpus_hash = canonical_source_manifest_sha256(
        (source.record for source in captured), policy
    )
    return CorpusSnapshot(tuple(captured), tuple(chunks), corpus_hash, policy)


def collect_corpus(
    vault: Path,
    *,
    daily_paths: Iterable[str | Path] = (),
    code_roots: Iterable[str | Path] = (),
    include_historical: bool = False,
    as_of: str | date | datetime | None = None,
    max_files: int = MAX_CORPUS_FILES,
    max_file_bytes: int = MAX_CORPUS_FILE_BYTES,
    max_total_bytes: int = MAX_CORPUS_TOTAL_BYTES,
    max_entries: int = MAX_CORPUS_INSPECTED_ENTRIES,
    max_directories: int = MAX_CORPUS_DIRECTORIES,
    max_depth: int = MAX_CORPUS_DEPTH,
    deadline: float | None = None,
    deadline_seconds: float | None = None,
    coordinator: object | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CorpusSnapshot:
    """Capture one immutable corpus and reject any change before returning it."""
    root = Path(vault).resolve(strict=True)
    if not stat.S_ISDIR(_safe_info(root).st_mode):
        raise ValueError("vault must be a regular directory")
    selected_policy = _policy(
        daily_paths=daily_paths,
        code_roots=code_roots,
        include_historical=include_historical,
        as_of=as_of,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_entries=max_entries,
        max_directories=max_directories,
        max_depth=max_depth,
    )
    selected_deadline = _deadline_value(deadline, deadline_seconds)
    if coordinator is not None:
        _check_deadline(selected_deadline)
        remaining = max(0.0, selected_deadline - time.monotonic())
        gate = coordinator.writer_gate(wait_seconds=remaining)
    else:
        gate = contextlib.nullcontext()
    with gate:
        _check_processing_stop(selected_deadline, cancelled)
        return _capture(root, selected_policy, selected_deadline, cancelled)


def validate_live_snapshot(
    snapshot: CorpusSnapshot,
    vault: Path,
    *,
    daily_paths: Iterable[str | Path] | None = None,
    code_roots: Iterable[str | Path] | None = None,
    include_historical: bool | None = None,
    as_of: str | date | datetime | None = None,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_entries: int | None = None,
    max_directories: int | None = None,
    max_depth: int | None = None,
    deadline: float | None = None,
    deadline_seconds: float | None = None,
    coordinator: object | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Rediscover and hash the live corpus immediately before publication."""
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    policy = snapshot.policy
    try:
        live = collect_corpus(
            vault,
            daily_paths=policy.daily_paths if daily_paths is None else daily_paths,
            code_roots=policy.code_roots if code_roots is None else code_roots,
            include_historical=(
                policy.include_historical
                if include_historical is None
                else include_historical
            ),
            as_of=policy.as_of if as_of is None else as_of,
            max_files=policy.max_files if max_files is None else max_files,
            max_file_bytes=(
                policy.max_file_bytes if max_file_bytes is None else max_file_bytes
            ),
            max_total_bytes=(
                policy.max_total_bytes if max_total_bytes is None else max_total_bytes
            ),
            max_entries=policy.max_entries if max_entries is None else max_entries,
            max_directories=(
                policy.max_directories if max_directories is None else max_directories
            ),
            max_depth=policy.max_depth if max_depth is None else max_depth,
            deadline=deadline,
            deadline_seconds=deadline_seconds,
            coordinator=coordinator,
            cancelled=cancelled,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise CorpusChanged("live corpus cannot reproduce captured membership") from exc
    if live.source_hashes != snapshot.source_hashes:
        raise CorpusChanged("live corpus membership or source hashes changed")
