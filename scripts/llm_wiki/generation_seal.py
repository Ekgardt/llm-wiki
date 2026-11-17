"""Sealed entries, held files and the seal capability of one generation tree."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import InitVar, dataclass, replace
from pathlib import Path

from llm_wiki import generation_descriptors, generation_limits
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.generation_descriptors import stable_descriptor_state
from llm_wiki.generation_fields import check_cancelled, check_monotonic_deadline
from llm_wiki.generation_registration import record_scanned_entry
from repository_scope import RepositoryScope

CANDIDATE_MINT = object()


@dataclass(frozen=True, order=True)
class EntrySeal:
    path: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    sha256: str | None


def same_regular_file(descriptor_stat: os.stat_result, path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return False
    return not generation_descriptors.is_link_or_reparse(path) and os.path.samestat(descriptor_stat, path_stat)


def _held_file_unchanged(held) -> bool:
    """The open descriptor still names the same regular file it was opened on."""
    descriptor_stat = os.fstat(held.descriptor)
    if not same_regular_file(descriptor_stat, held.path):
        return False
    return stable_descriptor_state(held.descriptor) == held.opened_state


@dataclass(frozen=True)
class HeldFile:
    path: Path
    relative: str
    descriptor: int
    opened_state: tuple[object, ...]


class GenerationSealCapability:
    """Bounded open-file capability for one coherent publication check."""

    def __init__(
        self,
        generation_path: Path,
        expected: tuple[EntrySeal, ...],
        held_files: tuple[HeldFile, ...],
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.generation_path = generation_path
        self.expected = expected
        self.held_files = held_files
        self.deadline = deadline
        self.monotonic = monotonic
        self.cancelled = cancelled
        self._closed = False

    def _held_files_unchanged(self) -> bool:
        for held in self.held_files:
            check_cancelled(self.cancelled)
            check_monotonic_deadline(self.deadline, self.monotonic)
            if not _held_file_unchanged(held):
                return False
        return True

    def _tree_unchanged(self) -> bool:
        current, files = scan_generation(
            self.generation_path,
            deadline=self.deadline,
            monotonic=self.monotonic,
            cancelled=self.cancelled,
        )
        expected_metadata = tuple(replace(entry, sha256=None) for entry in self.expected)
        expected_files = {entry.path for entry in self.expected if entry.kind == "file"}
        return current == expected_metadata and files == expected_files

    def revalidate(self) -> bool:
        check_cancelled(self.cancelled)
        check_monotonic_deadline(self.deadline, self.monotonic)
        if not self._tree_unchanged() or not self._held_files_unchanged():
            return False
        check_cancelled(self.cancelled)
        check_monotonic_deadline(self.deadline, self.monotonic)
        return True

    def close(self) -> None:
        if self._closed:
            return
        errors: list[OSError] = []
        for held in self.held_files:
            try:
                os.close(held.descriptor)
            except OSError as exc:
                errors.append(exc)
        self._closed = True
        if errors:
            raise OSError(
                f"{len(errors)} generation descriptor(s) failed to close: {errors[0]}"
            ) from errors[0]

    def __enter__(self) -> GenerationSealCapability:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class ValidatedCandidate:
    issuer: object
    generation_id: str
    generation_path: Path
    state_root: Path
    catalog_path: Path
    catalog_identity: EntrySeal
    manifest_bytes: bytes
    manifest_sha256: str
    repository_scope_bytes: bytes | None
    seal: tuple[EntrySeal, ...]
    mint: InitVar[object]

    def __post_init__(self, mint: object) -> None:
        if mint is not CANDIDATE_MINT:
            raise TypeError("validated candidates can only be minted by GenerationCatalog")


def validate_directory(path: Path, root: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise PermissionError("generation directory is outside the state root") from exc
    if generation_descriptors.is_link_or_reparse(path) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("generation directory must not be a link or reparse point")


def scope_from_manifest_bytes(encoded: bytes) -> RepositoryScope | None:
    """The repository scope a canonical manifest names; None when it is not canonical."""
    try:
        manifest = json.loads(encoded)
        if canonical_json_bytes(manifest) != encoded:
            return None
        return RepositoryScope.from_dict(manifest.get("repository_scope"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def _sealed_entry_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    raise PermissionError("generation members must be regular files or directories")


def entry_seal(path: Path, relative: str) -> EntrySeal:
    metadata = path.lstat()
    if generation_descriptors.is_link_or_reparse(path):
        raise PermissionError("generation members must not be links or reparse points")
    kind = _sealed_entry_kind(metadata.st_mode)
    return EntrySeal(
        path=relative,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=None,
    )


def bounded_scandir(
    path: Path,
    limit: int,
    message: str,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    with os.scandir(path) as iterator:
        for entry in iterator:
            _admit_scandir_entry(entries, entry, limit, message, deadline, monotonic, cancelled)
    check_monotonic_deadline(deadline, monotonic)
    entries.sort(key=lambda entry: entry.name)
    return entries


def _admit_scandir_entry(
    entries: list[os.DirEntry[str]],
    entry: os.DirEntry[str],
    limit: int,
    message: str,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    if len(entries) >= limit:
        raise ValueError(message)
    entries.append(entry)


def scan_generation(
    directory: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[EntrySeal, ...], set[str]]:
    check_cancelled(cancelled)
    check_monotonic_deadline(deadline, monotonic)
    files: set[str] = set()
    pending = [directory]
    seals = [entry_seal(directory, ".")]
    visited = 0
    limit = generation_limits.MAX_ARTIFACTS * 4 + 16
    while pending:
        current = pending.pop()
        ordered = bounded_scandir(
            current,
            limit - visited,
            "generation contains too many filesystem entries",
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        for entry in ordered:
            check_cancelled(cancelled)
            check_monotonic_deadline(deadline, monotonic)
            visited += 1
            path = Path(entry.path)
            relative = path.relative_to(directory).as_posix()
            seal = entry_seal(path, relative)
            seals.append(seal)
            record_scanned_entry(seal, path, relative, pending, files)
    check_monotonic_deadline(deadline, monotonic)
    return tuple(sorted(seals)), files


def listed_generation_files(directory: Path) -> set[str]:
    return scan_generation(directory)[1]


def _sealed_entry(entry: EntrySeal, digests: dict[str, str]) -> EntrySeal:
    if entry.kind != "file":
        return entry
    return replace(entry, sha256=digests[entry.path])


def append_unseen(ordered: list[str], seen: set[str], identifier: str) -> None:
    if identifier in seen:
        return
    seen.add(identifier)
    ordered.append(identifier)


def append_ancestors(
    ordered: list[str],
    seen: set[str],
    parents: dict[str, str | None],
    identifier: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    parent = parents.get(identifier)
    while parent is not None and parent not in seen:
        check_cancelled(cancelled)
        check_monotonic_deadline(deadline, time.monotonic)
        append_unseen(ordered, seen, parent)
        parent = parents.get(parent)


def metadata_only(expected: tuple[EntrySeal, ...]) -> tuple[EntrySeal, ...]:
    return tuple(replace(entry, sha256=None) for entry in expected)


def expected_file_entries(expected: tuple[EntrySeal, ...]) -> dict[str, EntrySeal]:
    return {entry.path: entry for entry in expected if entry.kind == "file"}


def content_seal(
    metadata_seal: tuple[EntrySeal, ...], digests: dict[str, str]
) -> tuple[EntrySeal, ...]:
    file_paths = {entry.path for entry in metadata_seal if entry.kind == "file"}
    if file_paths != set(digests):
        raise ValueError("content seal must bind every generation file")
    return tuple(_sealed_entry(entry, digests) for entry in metadata_seal)
