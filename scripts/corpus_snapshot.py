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
from vault_editorial import EDITORIAL_NAMES

COLLECTOR_VERSION = "corpus-collector/v1"
EXTRACTOR_VERSION = "markdown-heading-extractor/v1"

MAX_CORPUS_FILES = 10_000
MAX_CORPUS_FILE_BYTES = 8 * 1024 * 1024
MAX_CORPUS_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CORPUS_INSPECTED_ENTRIES = 50_000
MAX_CORPUS_DIRECTORIES = 5_000
MAX_CORPUS_DEPTH = 16
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
class CorpusSnapshot:
    sources: tuple[CapturedSource, ...]
    chunks: tuple[RetrievalChunk, ...]
    corpus_sha256: str
    policy: SnapshotPolicy
    collector_version: str = COLLECTOR_VERSION
    extractor_version: str = EXTRACTOR_VERSION

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


def _frontmatter(content: bytes) -> tuple[dict[str, Any], int]:
    text = content.decode("utf-8", errors="strict")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != b"---":
        return {}, 0
    end = None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == b"---":
            end = index
            break
    if end is None:
        raise ValueError("unterminated YAML frontmatter")
    frontmatter_bytes = b"".join(lines[1:end])
    value = yaml.safe_load(frontmatter_bytes.decode("utf-8", errors="strict"))
    if not isinstance(value, Mapping):
        raise ValueError("frontmatter must be a mapping")
    # Decode above is deliberate: invalid UTF-8 is rejected even outside frontmatter.
    del text
    return dict(value), sum(len(line) for line in lines[: end + 1])


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


def _clean_heading(raw: bytes | None) -> str:
    title = (raw or b"").decode("utf-8", errors="strict").strip()
    return _CLOSING_HASHES.sub("", title)


def _markdown_headings(content: bytes, start: int) -> list[re.Match[bytes]]:
    headings: list[re.Match[bytes]] = []
    fence_character: bytes | None = None
    fence_length = 0
    offset = start
    for line in content[start:].splitlines(keepends=True):
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
                    headings.append(heading)
        offset += len(line)
    return headings


def _chunks(
    source: SourceRecord,
    metadata: SourceMetadata,
    content: bytes,
    searchable_start: int,
    *,
    heading_enabled: bool = True,
) -> tuple[RetrievalChunk, ...]:
    headings = _markdown_headings(content, searchable_start) if heading_enabled else []
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    first_heading = headings[0].start() if headings else len(content)
    if content[searchable_start:first_heading].strip():
        spans.append((searchable_start, first_heading, ()))
    ancestry: list[tuple[int, str]] = []
    for index, heading in enumerate(headings):
        level = len(heading.group(1))
        title = _clean_heading(heading.group(2))
        ancestry = [item for item in ancestry if item[0] < level]
        ancestry.append((level, title))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        if content[heading.start():end].strip():
            spans.append((heading.start(), end, tuple(item[1] for item in ancestry)))
    result = []
    for start, end, heading_ancestry in spans:
        span = content[start:end]
        text = span.decode("utf-8", errors="strict")
        span_hash = _sha256(span)
        chunk_id = _canonical_hash(
            {
                "extractor": EXTRACTOR_VERSION,
                "parent": source.logical_id,
                "path": source.relative_path,
                "range": [start, end],
                "sha256": span_hash,
            }
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


def _capture(vault: Path, policy: SnapshotPolicy, deadline: float) -> CorpusSnapshot:
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
            frontmatter, searchable_start = _frontmatter(content)
        else:
            content.decode("utf-8", errors="strict")
            frontmatter, searchable_start = {}, 0
        metadata = _metadata(frontmatter, candidate)
        digest = _sha256(content)
        first_hashes[candidate.relative] = digest
        if not _included(metadata, policy):
            continue
        language = metadata.language or _infer_language(
            content[searchable_start:].decode("utf-8", errors="strict")
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
        chunks.extend(
            _chunks(
                record,
                metadata,
                content,
                searchable_start,
                heading_enabled=is_markdown,
            )
        )

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

    manifest = [
        {
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "logical_id": source.record.logical_id,
        }
        for source in captured
    ]
    corpus_hash = _canonical_hash(
        {
            "collector": COLLECTOR_VERSION,
            "extractor": EXTRACTOR_VERSION,
            "policy": {
                "daily_paths": policy.daily_paths,
                "code_roots": policy.code_roots,
                "include_historical": policy.include_historical,
                "as_of": policy.as_of,
            },
            "sources": manifest,
        }
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
        return _capture(root, selected_policy, selected_deadline)


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
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise CorpusChanged("live corpus cannot reproduce captured membership") from exc
    if live.source_hashes != snapshot.source_hashes:
        raise CorpusChanged("live corpus membership or source hashes changed")
