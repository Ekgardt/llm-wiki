"""Validated immutable generation registration and atomic activation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import InitVar, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from reliable_memory import (
    canonical_json_bytes,
    fsync_directory,
    open_operational_db,
    open_readonly_operational_db,
    read_runtime_bytes,
    restricted_relative_path,
    sha256_bytes,
    validate_runtime_file,
    validate_state_root,
)
from repository_scope import RepositoryScope

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        )

    class _FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
        )

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class _UnicodeString(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        )

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        )

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (
            ("status", ctypes.c_ssize_t),
            ("information", ctypes.c_size_t),
        )

    class _FileIdBothDirInfo(ctypes.Structure):
        _fields_ = (
            ("next_entry_offset", wintypes.DWORD),
            ("file_index", wintypes.DWORD),
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("end_of_file", ctypes.c_longlong),
            ("allocation_size", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
            ("file_name_length", wintypes.DWORD),
            ("ea_size", wintypes.DWORD),
            ("short_name_length", ctypes.c_byte),
            ("short_name", wintypes.WCHAR * 12),
            ("file_id", ctypes.c_longlong),
            ("file_name", wintypes.WCHAR * 1),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_file_information_by_handle_ex = _kernel32.GetFileInformationByHandleEx
    _get_file_information_by_handle_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _get_file_information_by_handle_ex.restype = wintypes.BOOL
    _get_file_information_by_handle = _kernel32.GetFileInformationByHandle
    _get_file_information_by_handle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information_by_handle.restype = wintypes.BOOL
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _nt_open_file = _ntdll.NtOpenFile
    _nt_open_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.ULONG,
        wintypes.ULONG,
    )
    _nt_open_file.restype = ctypes.c_long
    _rtl_nt_status_to_dos_error = _ntdll.RtlNtStatusToDosError
    _rtl_nt_status_to_dos_error.argtypes = (ctypes.c_long,)
    _rtl_nt_status_to_dos_error.restype = wintypes.ULONG

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACTS = 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_GENERATION_BYTES = 64 * 1024 * 1024 * 1024
MAX_GENERATION_CHILDREN = 4096
MAX_CATALOG_BYTES = 256 * 1024 * 1024
MAX_GENERATIONS = 1024
MAX_ACTIVATION_HISTORY = 16384
HASH_CHUNK_BYTES = 64 * 1024
# A caller with a deadline gets whatever is left of it, capped here. A caller
# without one waits out contention instead of surfacing `database is locked`:
# two writers doing a compare-and-swap on a loaded machine can hold the write
# lock for longer than five seconds.
BUSY_MS = 5000
UNBOUNDED_BUSY_MS = 30_000
CLEANUP_CATALOG_FENCE_SECONDS = 1.0

# How many superseded generations retention keeps behind the active one.
#
# One, and the number is reachability plus one fallback step, not a guess.
# Nothing reads a generation except through the active pointer: retrieval
# resolves it, and the incremental rebuild names the *active* generation as its
# reuse parent and reads `incremental-manifest.json` and the vectors out of that
# one tree. The single retained ancestor buys exactly one thing — it is the
# first alternative `_fallback_order` offers `_select_fallback` when the active
# tree stops validating, and it covers a reader that resolved the pointer just
# before the last activation. A generation two steps back buys nothing: it sits
# behind a younger candidate that is already ahead of it in the same order.
#
# This is rpm-ostree's two-deployment rule (current plus one rollback), not
# Kubernetes' ten revisions. Ten is affordable when a revision is metadata; a
# generation here is a ~180 MB tree, and 35 of them were 6.3 GB on a disk at
# 94%. See `docs/research/2026-08-29-how-many-superseded-generations-to-keep.md`.
RETAINED_ANCESTOR_GENERATIONS = 1

_GENERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_MANIFEST_KEYS = {
    "generation_id",
    "parent_generation_id",
    "schema_version",
    "collector_version",
    "extractor_version",
    "tokenizer_version",
    "tokenizer_config_sha256",
    "embedding_model_id",
    "embedding_model_revision",
    "vector_dimensions",
    "graph_schema_version",
    "graph_extractor_version",
    "source_manifest_sha256",
    "artifacts",
    "vector_state",
    "repository_scope",
    "code_capture",
}
_REQUIRED_MANIFEST_KEYS = _MANIFEST_KEYS - {
    "parent_generation_id",
    "repository_scope",
    "code_capture",
}
_ARTIFACT_KEYS = {"path", "size", "sha256"}
_VECTOR_FILES = {"vectors.npy", "vectors.json"}
_V2_REQUIRED_ARTIFACTS = {
    "source-manifest.json",
    "evidence.sqlite3",
    "search.sqlite3",
}
_V2_OPTIONAL_ARTIFACTS = {"incremental-manifest.json", *_VECTOR_FILES}
_CANDIDATE_MINT = object()


@dataclass(frozen=True, order=True)
class _EntrySeal:
    path: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    sha256: str | None


def _same_regular_file(descriptor_stat: os.stat_result, path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return False
    return not _is_link_or_reparse(path) and os.path.samestat(descriptor_stat, path_stat)


def _held_file_unchanged(held) -> bool:
    """The open descriptor still names the same regular file it was opened on."""
    descriptor_stat = os.fstat(held.descriptor)
    if not _same_regular_file(descriptor_stat, held.path):
        return False
    return _stable_descriptor_state(held.descriptor) == held.opened_state


@dataclass(frozen=True)
class _HeldFile:
    path: Path
    relative: str
    descriptor: int
    opened_state: tuple[object, ...]


class _GenerationSealCapability:
    """Bounded open-file capability for one coherent publication check."""

    def __init__(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        held_files: tuple[_HeldFile, ...],
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
            _check_cancelled(self.cancelled)
            _check_deadline(self.deadline, self.monotonic)
            if not _held_file_unchanged(held):
                return False
        return True

    def _tree_unchanged(self) -> bool:
        current, files = _scan_generation(
            self.generation_path,
            deadline=self.deadline,
            monotonic=self.monotonic,
            cancelled=self.cancelled,
        )
        expected_metadata = tuple(replace(entry, sha256=None) for entry in self.expected)
        expected_files = {entry.path for entry in self.expected if entry.kind == "file"}
        return current == expected_metadata and files == expected_files

    def revalidate(self) -> bool:
        _check_cancelled(self.cancelled)
        _check_deadline(self.deadline, self.monotonic)
        if not self._tree_unchanged() or not self._held_files_unchanged():
            return False
        _check_cancelled(self.cancelled)
        _check_deadline(self.deadline, self.monotonic)
        return True

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        for held in self.held_files:
            try:
                os.close(held.descriptor)
            except BaseException as exc:
                errors.append(exc)
        self._closed = True
        if errors:
            raise errors[0]

    def __enter__(self) -> _GenerationSealCapability:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class _ValidatedCandidate:
    issuer: object
    generation_id: str
    generation_path: Path
    state_root: Path
    catalog_path: Path
    catalog_identity: _EntrySeal
    manifest_bytes: bytes
    manifest_sha256: str
    repository_scope_bytes: bytes | None
    seal: tuple[_EntrySeal, ...]
    mint: InitVar[object]

    def __post_init__(self, mint: object) -> None:
        if mint is not _CANDIDATE_MINT:
            raise TypeError("validated candidates can only be minted by GenerationCatalog")


def _safe_path_component(value: str) -> bool:
    if _GENERATION_RE.fullmatch(value) is None:
        return False
    return value not in {".", ".."} and value[-1] not in {".", " "}


def _generation_id(value: object) -> str:
    if not isinstance(value, str) or not _safe_path_component(value):
        raise ValueError("generation_id must be an exact safe path component")
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise ValueError("generation_id is a reserved path component")
    return value


def _bounded_version_text(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    return not any(character in value for character in "\x00\r\n")


def _bounded_version(name: str, value: object) -> str:
    if not _bounded_version_text(value):
        raise ValueError(f"{name} must be a bounded non-empty string")
    try:
        str(value).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid Unicode") from exc
    return str(value)


def _optional_version(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _bounded_version(name, value)


def _parsed_clock_text(value: str) -> datetime:
    if len(value) > 40 or any(character in value for character in "\x00\r\n"):
        raise ValueError("clock returned an invalid timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("clock must return an ISO-8601 timestamp") from exc


def _parsed_clock(value: object) -> datetime:
    if isinstance(value, str):
        return _parsed_clock_text(value)
    if isinstance(value, datetime):
        return value
    raise TypeError("clock must return a datetime or ISO-8601 string")


def _utc_timestamp(clock: Callable[[], datetime | str]) -> str:
    parsed = _parsed_clock(clock())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("clock timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _validate_directory(path: Path, root: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise PermissionError("generation directory is outside the state root") from exc
    if _is_link_or_reparse(path) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("generation directory must not be a link or reparse point")


def _entry_seal(path: Path, relative: str) -> _EntrySeal:
    metadata = path.lstat()
    if _is_link_or_reparse(path):
        raise PermissionError("generation members must not be links or reparse points")
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        raise PermissionError("generation members must be regular files or directories")
    return _EntrySeal(
        path=relative,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=None,
    )


def _check_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("generation catalog deadline reached")


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if bool(cancelled and cancelled()):
        raise TimeoutError("generation catalog operation cancelled")


def _bounded_scandir(
    path: Path,
    limit: int,
    message: str,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    _check_cancelled(cancelled)
    _check_deadline(deadline, monotonic)
    with os.scandir(path) as iterator:
        for entry in iterator:
            _check_cancelled(cancelled)
            _check_deadline(deadline, monotonic)
            if len(entries) >= limit:
                raise ValueError(message)
            entries.append(entry)
    _check_deadline(deadline, monotonic)
    entries.sort(key=lambda entry: entry.name)
    return entries


def _scan_generation(
    directory: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[_EntrySeal, ...], set[str]]:
    _check_cancelled(cancelled)
    _check_deadline(deadline, monotonic)
    files: set[str] = set()
    pending = [directory]
    seals = [_entry_seal(directory, ".")]
    visited = 0
    limit = MAX_ARTIFACTS * 4 + 16
    while pending:
        current = pending.pop()
        ordered = _bounded_scandir(
            current,
            limit - visited,
            "generation contains too many filesystem entries",
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        for entry in ordered:
            _check_cancelled(cancelled)
            _check_deadline(deadline, monotonic)
            visited += 1
            path = Path(entry.path)
            relative = path.relative_to(directory).as_posix()
            seal = _entry_seal(path, relative)
            seals.append(seal)
            _record_scanned_entry(seal, path, relative, pending, files)
    _check_deadline(deadline, monotonic)
    return tuple(sorted(seals)), files


def _listed_generation_files(directory: Path) -> set[str]:
    return _scan_generation(directory)[1]


def _sealed_entry(entry: _EntrySeal, digests: dict[str, str]) -> _EntrySeal:
    if entry.kind != "file":
        return entry
    return replace(entry, sha256=digests[entry.path])


def _append_unseen(ordered: list[str], seen: set[str], identifier: str) -> None:
    if identifier in seen:
        return
    seen.add(identifier)
    ordered.append(identifier)


def _append_ancestors(
    ordered: list[str],
    seen: set[str],
    parents: dict[str, str | None],
    identifier: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    parent = parents.get(identifier)
    while parent is not None and parent not in seen:
        _check_cancelled(cancelled)
        _check_deadline(deadline, time.monotonic)
        _append_unseen(ordered, seen, parent)
        parent = parents.get(parent)


def _metadata_only(expected: tuple[_EntrySeal, ...]) -> tuple[_EntrySeal, ...]:
    return tuple(replace(entry, sha256=None) for entry in expected)


def _expected_file_entries(expected: tuple[_EntrySeal, ...]) -> dict[str, _EntrySeal]:
    return {entry.path: entry for entry in expected if entry.kind == "file"}


def _pointer_is(database: sqlite3.Connection, expected_active: str | None) -> bool:
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    return row is not None and row["active_generation_id"] == expected_active


def _pointer_matches(database: sqlite3.Connection, active: str | None) -> bool:
    """Like `_pointer_is`, but a missing pointer row is corruption, not a mismatch."""
    current = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    if current is None:
        raise ValueError("catalog active pointer is missing")
    return current["active_generation_id"] == active


def _record_activation(
    database: sqlite3.Connection, selected_id: str | None, timestamp: str
) -> None:
    """Clearing the pointer records nothing; pointing it somewhere records that."""
    if selected_id is None:
        return
    database.execute(
        "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
        (selected_id, timestamp),
    )


def _generation_in_use(database: sqlite3.Connection, identifier: str) -> bool:
    """Active now, or ever activated: either way the registration must be kept."""
    if _pointer_matches(database, identifier):
        return True
    historical = database.execute(
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier,),
    ).fetchone()
    return historical is not None


def _active_generation(database: sqlite3.Connection) -> str | None:
    """The generation the pointer names; a missing pointer row is corruption."""
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise ValueError("catalog active pointer is missing")
    return row["active_generation_id"]


def _parent_generation(database: sqlite3.Connection, identifier: str) -> str | None:
    row = database.execute(
        "SELECT parent_generation_id FROM generations WHERE generation_id = ?",
        (identifier,),
    ).fetchone()
    if row is None:
        return None
    return row["parent_generation_id"]


def _retained_generations(database: sqlite3.Connection, ancestors: int) -> tuple[str, ...]:
    """The live set: the active generation plus the ancestors retention keeps.

    Reachability from the active pointer along `parent_generation_id`, bounded
    by depth. A cycle or a dangling parent ends the walk rather than extending
    it, so a damaged chain shrinks the live set to what it can still prove.
    """
    active = _active_generation(database)
    if active is None:
        return ()
    retained = [active]
    identifier: str | None = active
    for _ in range(ancestors):
        identifier = _parent_generation(database, identifier)
        if identifier is None or identifier in retained:
            break
        retained.append(identifier)
    return tuple(retained)


def _require_active_root(retained: tuple[str, ...]) -> None:
    """No active pointer is no root, and without a root nothing is provably
    unreachable. `_repair_active_pointer` clears the pointer when no candidate
    validates; collecting then would destroy the very material a repair reads.
    That state is for a person to look at, not for a collector to act on."""
    if not retained:
        raise ValueError("catalog has no active generation")


def _require_activated(database: sqlite3.Connection, identifier: str) -> None:
    """Only a superseded generation is prunable.

    A registration that has never been activated is either an abandoned
    publication or one happening right now, and nothing readable here tells the
    two apart: `register` returns before `activate` is called, so a build in
    flight looks exactly like an abort. `discard_unactivated` is the path for
    that case, and it runs under the publication fence.
    """
    activated = database.execute(
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier,),
    ).fetchone()
    if activated is None:
        raise ValueError("generation was never activated")


def _require_retained_ancestors(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("retained_ancestors must be a non-negative integer")
    if value < 0:
        raise ValueError("retained_ancestors must be a non-negative integer")


def _decoded_manifest(encoded: bytes) -> dict[str, object] | None:
    """The manifest object these bytes canonically encode, or None."""
    try:
        manifest = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != encoded:
        return None
    return manifest


def _verified_manifest(row: sqlite3.Row) -> dict[str, object] | None:
    """A registration row's manifest when its bytes still prove themselves."""
    encoded = bytes(row["manifest_json"])
    if sha256_bytes(encoded) != row["manifest_sha256"]:
        return None
    return _decoded_manifest(encoded)


def _manifest_belongs_to(
    manifest: dict[str, object], repository_scope: RepositoryScope
) -> bool:
    """Whether this manifest's own scope names the same repository checkout.

    Identity only: a generation built at another commit of the same checkout
    still belongs to it. See `RepositoryScope.identity` and NEW-65.
    """
    try:
        registered = RepositoryScope.from_dict(manifest.get("repository_scope"))
    except (TypeError, ValueError):
        return False
    return registered.same_repository(repository_scope)


def _record_scanned_entry(seal, path: Path, relative: str, pending: list, files: set) -> None:
    if seal.kind == "directory":
        pending.append(path)
        return
    files.add(relative)


def _close_descriptors(held_files: list) -> None:
    for held in held_files:
        try:
            os.close(held.descriptor)
        except OSError:
            pass


def _require_registration_match(row, encoded: bytes, digest: str) -> None:
    if row is None:
        return
    if bytes(row["manifest_json"]) != encoded or row["manifest_sha256"] != digest:
        raise ValueError("generation registration is immutable")


def _require_registration_token(registered, token: tuple[bytes, str]) -> None:
    if registered is None:
        raise ValueError("generation registration disappeared")
    stored = (bytes(registered["manifest_json"]), registered["manifest_sha256"])
    if stored != token:
        raise ValueError("generation registration changed after validation")


def _require_fallback_registration(registered, token: tuple[bytes, str] | None) -> None:
    if registered is None:
        raise ValueError("fallback registration changed after validation")
    stored = (bytes(registered["manifest_json"]), registered["manifest_sha256"])
    if stored != token:
        raise ValueError("fallback registration changed after validation")


def _selected_token(
    selected: dict[str, object] | None, selected_id: str | None
) -> tuple[bytes, str] | None:
    if selected_id is None or selected is None:
        return None
    encoded = canonical_json_bytes(selected)
    return encoded, sha256_bytes(encoded)


def _still_referenced(database: sqlite3.Connection, identifier: str) -> bool:
    referenced = database.execute(
        "SELECT 1 FROM generations WHERE generation_id = ? "
        "UNION ALL "
        "SELECT 1 FROM catalog_state "
        "WHERE singleton = 1 AND active_generation_id = ? "
        "UNION ALL "
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier, identifier, identifier),
    ).fetchone()
    return referenced is not None


def _candidate_manifest(candidate) -> dict[str, object]:
    """The manifest the candidate carries, still canonical and still its own."""
    if sha256_bytes(candidate.manifest_bytes) != candidate.manifest_sha256:
        raise ValueError("validated candidate manifest hash changed")
    try:
        manifest = json.loads(candidate.manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("validated candidate manifest is invalid") from exc
    _require_canonical_candidate(manifest, candidate)
    scope = _manifest_scope(manifest)
    scope_bytes = None if scope is None else canonical_json_bytes(scope.as_dict())
    if scope_bytes != candidate.repository_scope_bytes:
        raise ValueError("validated candidate repository scope changed")
    return manifest


def _canonical_candidate(manifest: object, candidate) -> bool:
    """Order matters: the byte comparison is only meaningful for a dict."""
    return (
        isinstance(manifest, dict)
        and canonical_json_bytes(manifest) == candidate.manifest_bytes
        and manifest.get("generation_id") == candidate.generation_id
    )


def _require_canonical_candidate(manifest: object, candidate) -> None:
    if not _canonical_candidate(manifest, candidate):
        raise ValueError("validated candidate manifest is not canonical")


def _manifest_scope(manifest: dict[str, object]) -> RepositoryScope | None:
    scope_value = manifest.get("repository_scope")
    if scope_value is None:
        return None
    return RepositoryScope.from_dict(scope_value)


def _require_publication_scope(
    scope: RepositoryScope | None, expected: RepositoryScope | None
) -> None:
    """Publication still binds the exact scope, commit included: it records what
    the generation was built from. Eligibility later asks a weaker question."""
    if expected is None:
        return
    if scope != RepositoryScope.from_dict(expected.as_dict()):
        raise ValueError("generation repository scope does not match publication scope")


def _raise_writer_timeout(exc: sqlite3.OperationalError, deadline: float | None) -> None:
    if deadline is not None and "locked" in str(exc).casefold():
        raise TimeoutError("generation catalog writer deadline reached") from exc


def _require_catalog_bound(max_catalog_bytes: object) -> None:
    if isinstance(max_catalog_bytes, bool) or not isinstance(max_catalog_bytes, int):
        raise ValueError("max_catalog_bytes must be a positive integer")
    if max_catalog_bytes < 1:
        raise ValueError("max_catalog_bytes must be a positive integer")


def _require_inside_state_root(catalog_path: Path, state_root: Path) -> None:
    try:
        catalog_path.relative_to(state_root)
    except ValueError as exc:
        raise ValueError("catalog_path must remain inside state_root") from exc


def _valid_page_metric(value: object, minimum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= minimum


def _finite_number(value: object) -> bool:
    """A real finite number; a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _require_absolute_deadline(deadline: object) -> None:
    if deadline is None:
        return
    if not _finite_number(deadline):
        raise ValueError("deadline must be an absolute monotonic timestamp or None")


def _content_seal(
    metadata_seal: tuple[_EntrySeal, ...], digests: dict[str, str]
) -> tuple[_EntrySeal, ...]:
    file_paths = {entry.path for entry in metadata_seal if entry.kind == "file"}
    if file_paths != set(digests):
        raise ValueError("content seal must bind every generation file")
    return tuple(_sealed_entry(entry, digests) for entry in metadata_seal)


def _stable_file_stat(metadata: os.stat_result) -> tuple[int, ...]:
    """Return identity and mutation-sensitive fields unaffected by reads."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        getattr(metadata, "st_file_attributes", 0),
        getattr(metadata, "st_reparse_tag", 0),
    )


def _require_win32(succeeded: object) -> None:
    """Raise the last Win32 error when an API reports failure."""
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_basic_information(handle: int) -> _FileBasicInfo:
    information = _FileBasicInfo()
    _require_win32(
        _get_file_information_by_handle_ex(
            handle,
            0,  # FileBasicInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )
    return information


def _windows_file_id_information(handle: int) -> _FileIdInfo:
    information = _FileIdInfo()
    _require_win32(
        _get_file_information_by_handle_ex(
            handle,
            18,  # FileIdInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )
    return information


def _windows_by_handle_information(handle: int) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    _require_win32(_get_file_information_by_handle(handle, ctypes.byref(information)))
    return information


def _windows_change_time(descriptor: int) -> int | None:
    if os.name != "nt":
        return None
    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("invalid Windows file handle")
    return int(_windows_basic_information(handle).change_time)


def _windows_handle_file_identity(handle: int) -> tuple[str, int, bytes]:
    if os.name != "nt":
        raise OSError("Windows handle identity is unavailable")
    information = _windows_file_id_information(handle)
    file_id = bytes(information.file_id.identifier)
    volume = int(information.volume_serial_number)
    if volume <= 0 or not any(file_id):
        raise OSError("Windows stable file identity is unavailable")
    return ("windows", volume, file_id)


def _windows_stat_matches_identity(
    metadata: os.stat_result, identity: tuple[int, int]
) -> bool:
    values = (
        getattr(metadata, "st_dev", 0),
        getattr(metadata, "st_ino", 0),
        *identity,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        return False
    return values[:2] == values[2:]


def _windows_file_id_identity(handle: int) -> tuple[int, int]:
    """The identity `os.stat` reports on Python 3.12 and later.

    Python 3.12 widened Windows `st_dev` to 64 bits and `st_ino` to 128 bits by
    reading `FILE_ID_INFO` (gh-99726). `GetFileInformationByHandle` still
    reports the 32-bit volume serial of `BY_HANDLE_FILE_INFORMATION`, so
    comparing one against the other rejected a directory that had not changed,
    and repository capture failed with "changed during enumeration" on every
    Windows runner from 3.12 onward.
    """
    _, volume, file_id = _windows_handle_file_identity(handle)
    return volume, int.from_bytes(file_id, "little")


def _windows_handle_identity_candidates(handle: int) -> tuple[tuple[int, int], ...]:
    """Both identities the kernel reports for one open handle.

    Which of them `os.stat` returns depends on the running interpreter, not on
    the file, so a caller proving "the entry I enumerated is the object I
    opened" has to accept either. Both are read from the same handle, so this
    stays an exact identity match rather than a weaker one.
    """
    candidates = [_windows_handle_stat_identity(handle)]
    try:
        candidates.append(_windows_file_id_identity(handle))
    except OSError:
        return tuple(candidates)
    return tuple(candidates)


def _windows_stat_matches_any_identity(
    metadata: os.stat_result, candidates: tuple[tuple[int, int], ...]
) -> bool:
    return any(
        _windows_stat_matches_identity(metadata, candidate) for candidate in candidates
    )


def _windows_handle_stat_identity(handle: int) -> tuple[int, int]:
    if os.name != "nt":
        raise OSError("Windows handle stat identity is unavailable")
    information = _windows_by_handle_information(handle)
    volume = int(information.volume_serial_number)
    file_index = (int(information.file_index_high) << 32) | int(
        information.file_index_low
    )
    if volume <= 0 or file_index <= 0:
        raise OSError("Windows stable handle stat identity is unavailable")
    return volume, file_index


def _windows_object_attributes(parent_handle: int, name: str):
    name_buffer = ctypes.create_unicode_buffer(name)
    name_bytes = len(name.encode("utf-16-le"))
    object_name = _UnicodeString(
        name_bytes,
        name_bytes + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent_handle,
        ctypes.pointer(object_name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    return attributes, object_name, name_buffer


def _windows_desired_access(directory: bool) -> int:
    access = 0x00100000 | 0x00000080  # SYNCHRONIZE | FILE_READ_ATTRIBUTES
    if directory:
        return access | 0x00000001  # FILE_LIST_DIRECTORY
    return access | 0x80000000  # GENERIC_READ


def _windows_open_options(directory: bool) -> int:
    kind = 0x00000001 if directory else 0x00000040  # DIRECTORY | NON_DIRECTORY
    return 0x00200000 | 0x00000020 | kind  # REPARSE_POINT | SYNCHRONOUS_IO_NONALERT


def _require_windows_component_kind(value: int, directory: bool) -> None:
    information = _windows_by_handle_information(value)
    if information.file_attributes & 0x00000400:
        raise PermissionError("Windows relative path component is a reparse point")
    if bool(information.file_attributes & 0x00000010) != directory:
        raise PermissionError("Windows relative path component has the wrong kind")


def _require_single_component(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Windows relative handle name must be one path component")


def _windows_open_relative_handle(
    parent_handle: int, name: str, *, directory: bool
) -> int:
    if os.name != "nt":
        raise OSError("Windows relative handle open is unavailable")
    _require_single_component(name)
    attributes, _object_name, _buffer = _windows_object_attributes(parent_handle, name)
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    status = _nt_open_file(
        ctypes.byref(handle),
        _windows_desired_access(directory),
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        0x00000001 | 0x00000002 | 0x00000004,
        _windows_open_options(directory),
    )
    if status < 0:
        error = int(_rtl_nt_status_to_dos_error(status))
        raise OSError(error, f"cannot open Windows relative path component: {name}")
    value = int(handle.value)
    try:
        _require_windows_component_kind(value, directory)
    except BaseException:
        _close_handle(value)
        raise
    return value


def _windows_relative_file_descriptor(parent_handle: int, name: str) -> int:
    handle = _windows_open_relative_handle(parent_handle, name, directory=False)
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        _close_handle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _windows_entry_kind(attributes: int) -> str:
    if attributes & 0x00000400:
        return "link"
    if attributes & 0x00000010:
        return "directory"
    return "file"


def _windows_entry_name(buffer, offset: int, name_offset: int, buffer_size: int) -> str:
    information = _FileIdBothDirInfo.from_buffer(buffer, offset)
    name_length = int(information.file_name_length)
    if name_length <= 0 or name_length % 2:
        raise OSError("Windows directory enumeration returned invalid data")
    if offset + name_offset + name_length > buffer_size:
        raise OSError("Windows directory enumeration returned invalid data")
    return ctypes.wstring_at(
        ctypes.addressof(buffer) + offset + name_offset, name_length // 2
    )


def _windows_next_offset(
    buffer, offset: int, name_offset: int, buffer_size: int
) -> int | None:
    information = _FileIdBothDirInfo.from_buffer(buffer, offset)
    next_offset = int(information.next_entry_offset)
    if next_offset == 0:
        return None
    if next_offset < name_offset or offset + next_offset >= buffer_size:
        raise OSError("Windows directory enumeration returned invalid offsets")
    return offset + next_offset


def _windows_buffer_entries(
    buffer, name_offset: int, buffer_size: int, entries: list, max_entries: int
) -> None:
    offset: int | None = 0
    while offset is not None:
        name = _windows_entry_name(buffer, offset, name_offset, buffer_size)
        information = _FileIdBothDirInfo.from_buffer(buffer, offset)
        _append_windows_entry(entries, name, int(information.file_attributes), max_entries)
        offset = _windows_next_offset(buffer, offset, name_offset, buffer_size)


def _append_windows_entry(
    entries: list, name: str, attributes: int, max_entries: int
) -> None:
    if name in {".", ".."}:
        return
    entries.append((name, _windows_entry_kind(attributes)))
    if len(entries) > max_entries:
        raise ValueError("repository code entry limit exceeded")


def _windows_fill_buffer(handle: int, buffer, buffer_size: int, restart: bool) -> bool:
    """False once the enumeration is exhausted."""
    information_class = 11 if restart else 10
    if _get_file_information_by_handle_ex(
        handle, information_class, ctypes.byref(buffer), buffer_size
    ):
        return True
    error = ctypes.get_last_error()
    if error in {18, 38}:  # ERROR_NO_MORE_FILES | ERROR_HANDLE_EOF
        return False
    raise ctypes.WinError(error)


def _require_entry_bound(max_entries: object) -> None:
    if isinstance(max_entries, bool) or not isinstance(max_entries, int):
        raise ValueError("max_entries must be a non-negative integer")
    if max_entries < 0:
        raise ValueError("max_entries must be a non-negative integer")


def _windows_list_directory(handle: int, *, max_entries: int) -> list[tuple[str, str]]:
    if os.name != "nt":
        raise OSError("Windows handle directory enumeration is unavailable")
    _require_entry_bound(max_entries)
    entries: list[tuple[str, str]] = []
    restart = True
    buffer_size = 64 * 1024
    name_offset = _FileIdBothDirInfo.file_name.offset
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        if not _windows_fill_buffer(handle, buffer, buffer_size, restart):
            return entries
        _windows_buffer_entries(buffer, name_offset, buffer_size, entries, max_entries)
        restart = False


def _posix_file_identity(descriptor: int) -> tuple[object, ...]:
    metadata = os.fstat(descriptor)
    if metadata.st_dev <= 0 or metadata.st_ino <= 0:
        raise OSError("POSIX stable file identity is unavailable")
    return ("posix", metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _descriptor_file_identity(descriptor: int) -> tuple[object, ...]:
    if os.name == "nt":
        handle = msvcrt.get_osfhandle(descriptor)
        if handle == -1:
            raise OSError("invalid Windows file handle")
        return _windows_handle_file_identity(handle)
    if os.name == "posix":
        return _posix_file_identity(descriptor)
    raise OSError("stable file identity is unavailable")


def _stable_descriptor_state(descriptor: int) -> tuple[object, ...]:
    return (
        *_stable_file_stat(os.fstat(descriptor)),
        _descriptor_file_identity(descriptor),
        _windows_change_time(descriptor),
    )


def _windows_long_path(path: Path) -> str:
    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _windows_read_handle(path: Path) -> int:
    handle = _create_file(
        _windows_long_path(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read, write, and delete
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _closed_on_failure(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _windows_read_descriptor(path: Path) -> int:
    handle = _windows_read_handle(path)
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        _close_handle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        _closed_on_failure(descriptor)
        raise
    return descriptor


def _open_read_descriptor(path: Path) -> int:
    if os.name == "nt":
        return _windows_read_descriptor(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _hash_descriptor(
    descriptor: int,
    max_bytes: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    _check_cancelled(cancelled)
    _check_deadline(deadline, monotonic)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        _check_cancelled(cancelled)
        _check_deadline(deadline, monotonic)
        chunk = os.read(descriptor, HASH_CHUNK_BYTES)
        _check_deadline(deadline, monotonic)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("artifact exceeds its declared size")
        digest.update(chunk)
    _check_cancelled(cancelled)
    _check_deadline(deadline, monotonic)
    return total, digest.hexdigest()


def _hashed_descriptor(
    descriptor: int,
    expected: os.stat_result,
    max_bytes: int,
    **stop: object,
) -> tuple[int, str, os.stat_result]:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
        raise PermissionError("artifact identity changed while opening")
    opened_state = _stable_descriptor_state(descriptor)
    total, digest = _hash_descriptor(descriptor, max_bytes, **stop)
    after = os.fstat(descriptor)
    if opened_state != _stable_descriptor_state(descriptor):
        raise PermissionError("artifact changed while hashing")
    return total, digest, after


def _hash_artifact(
    path: Path,
    state_root: Path,
    max_bytes: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    _check_cancelled(cancelled)
    _check_deadline(deadline, monotonic)
    expected = validate_runtime_file(path, state_root, max_bytes=max_bytes)
    descriptor = _open_read_descriptor(path)
    try:
        total, digest, after = _hashed_descriptor(
            descriptor,
            expected,
            max_bytes,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _is_link_or_reparse(path) or not os.path.samestat(after, current):
        raise PermissionError("artifact identity changed after hashing")
    _check_deadline(deadline, monotonic)
    _check_cancelled(cancelled)
    return total, digest


def _manifest_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest.json must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("generation manifest must be an object")
    return value


def _require_manifest_keys(value: dict[str, object]) -> None:
    keys = set(value)
    if not _REQUIRED_MANIFEST_KEYS <= keys or not keys <= _MANIFEST_KEYS:
        raise ValueError("generation manifest has missing or unknown properties")


def _require_manifest_shape(value: dict[str, object], raw: bytes, expected_id: str) -> None:
    _require_manifest_keys(value)
    if canonical_json_bytes(value) != raw:
        raise ValueError("generation manifest must use canonical JSON")
    if _generation_id(str(value["generation_id"])) != expected_id:
        raise ValueError("generation_id must equal the generation directory name")


def _manifest_parent(value: dict[str, object], expected_id: str) -> str | None:
    parent = value.get("parent_generation_id")
    if parent is None:
        return None
    parent = _generation_id(str(parent))
    if parent == expected_id:
        raise ValueError("generation cannot be its own parent")
    return parent


def _normalized_versions(value: dict[str, object], expected_id: str) -> dict[str, object]:
    return {
        "generation_id": expected_id,
        "schema_version": _bounded_version("schema_version", value["schema_version"]),
        "collector_version": _bounded_version("collector_version", value["collector_version"]),
        "extractor_version": _bounded_version("extractor_version", value["extractor_version"]),
        "tokenizer_version": _bounded_version("tokenizer_version", value["tokenizer_version"]),
    }


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _bounded_dimension(dimensions: object) -> bool:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        return False
    return 1 <= dimensions <= 65536


def _validated_dimensions(dimensions: object) -> int | None:
    if dimensions is None:
        return None
    if not _bounded_dimension(dimensions):
        raise ValueError("vector_dimensions must be null or a positive bounded integer")
    return dimensions


def _embedding_fields(
    value: dict[str, object]
) -> tuple[str | None, str | None, int | None, tuple[bool, bool, bool]]:
    embedding_id = _optional_version("embedding_model_id", value["embedding_model_id"])
    embedding_revision = _optional_version(
        "embedding_model_revision", value["embedding_model_revision"]
    )
    dimensions = _validated_dimensions(value["vector_dimensions"])
    present = (
        embedding_id is not None,
        embedding_revision is not None,
        dimensions is not None,
    )
    if any(present) and not all(present):
        raise ValueError("embedding model ID, revision, and dimensions must be all set or null")
    return embedding_id, embedding_revision, dimensions, present


def _graph_fields(value: dict[str, object]) -> tuple[str | None, str | None]:
    graph_schema = _optional_version("graph_schema_version", value["graph_schema_version"])
    graph_extractor = _optional_version(
        "graph_extractor_version", value["graph_extractor_version"]
    )
    if graph_schema not in {None, "evidence-graph/v2", "evidence-graph/v3"}:
        raise ValueError("graph schema must be null, evidence-graph/v2, or evidence-graph/v3")
    if (graph_schema is None) != (graph_extractor is None):
        raise ValueError("graph schema and extractor versions must be both set or null")
    return graph_schema, graph_extractor


def _normalized_parent_section(_value: dict[str, object], parent: str | None) -> object:
    return parent


def _normalized_scope_section(value: dict[str, object], _parent: str | None) -> object:
    return RepositoryScope.from_dict(value["repository_scope"]).as_dict()


def _normalized_capture_section(value: dict[str, object], _parent: str | None) -> object:
    from code_workspace import validate_code_capture

    return validate_code_capture(value["code_capture"])


# Insertion order is the manifest's own section order and is load-bearing only
# for readability; canonical JSON sorts keys before anything is hashed.
_OPTIONAL_MANIFEST_SECTIONS = {
    "parent_generation_id": _normalized_parent_section,
    "repository_scope": _normalized_scope_section,
    "code_capture": _normalized_capture_section,
}


def _apply_optional_sections(
    normalized: dict[str, object], value: dict[str, object], parent: str | None
) -> None:
    for key, normalize in _OPTIONAL_MANIFEST_SECTIONS.items():
        if key in value:
            normalized[key] = normalize(value, parent)


def _artifact_relative_path(artifact: dict[str, object], seen: set[str]) -> str:
    path_value = artifact["path"]
    if not isinstance(path_value, str):
        raise TypeError("artifact path must be a string")
    first = path_value.split("/", 1)[0]
    path_text = restricted_relative_path(path_value, (first,)).as_posix()
    if path_text == "manifest.json" or path_text in seen:
        raise ValueError("artifact paths must be unique and exclude manifest.json")
    return path_text


def _artifact_size(artifact: dict[str, object]) -> int:
    size = artifact["size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("artifact size is outside the supported bounds")
    if not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise ValueError("artifact size is outside the supported bounds")
    return size


class _ArtifactScan:
    """Every artifact hashed against the manifest, under the generation bounds."""

    def __init__(self, generation_path: Path, state_root: Path) -> None:
        self.generation_path = generation_path
        self.state_root = state_root
        self.seen: set[str] = set()
        self.normalized: list[dict[str, object]] = []
        self.digests: dict[str, str] = {}
        self.total = 0

    def add(self, artifact: object, **stop: object) -> None:
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            raise ValueError("each artifact must be a closed object")
        path_text = _artifact_relative_path(artifact, self.seen)
        size = self._counted_size(artifact)
        digest = _required_sha256(artifact["sha256"], "artifact sha256")
        self._verify(path_text, size, digest, **stop)
        self.seen.add(path_text)
        self.normalized.append({"path": path_text, "size": size, "sha256": digest})

    def _counted_size(self, artifact: dict[str, object]) -> int:
        size = _artifact_size(artifact)
        self.total += size
        if self.total > MAX_GENERATION_BYTES:
            raise ValueError("generation artifact bytes exceed the supported bound")
        return size

    def _verify(self, path_text: str, size: int, digest: str, **stop: object) -> None:
        artifact_path = self.generation_path.joinpath(*PurePosixPath(path_text).parts)
        actual_size, actual_digest = _hash_artifact(
            artifact_path, self.state_root, size, **stop
        )
        if actual_size != size:
            raise ValueError(f"artifact has wrong size: {path_text}")
        if actual_digest != digest:
            raise ValueError(f"artifact has wrong hash: {path_text}")
        self.digests[path_text] = actual_digest


def _scan_artifacts(
    artifacts: object, generation_path: Path, state_root: Path, **stop: object
) -> _ArtifactScan:
    _require_artifact_list(artifacts)
    scan = _ArtifactScan(generation_path, state_root)
    for artifact in artifacts:
        _check_deadline(stop.get("deadline"), stop.get("monotonic", time.monotonic))
        scan.add(artifact, **stop)
    scan.normalized.sort(key=lambda item: str(item["path"]))
    if artifacts != scan.normalized:
        raise ValueError("artifacts must be normalized and sorted by path")
    return scan


def _require_artifact_list(artifacts: object) -> None:
    if not isinstance(artifacts, list):
        raise TypeError("artifacts must be an array")
    if not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise ValueError("artifact count is outside the supported bounds")


def _require_vector_set(
    present_vectors: set[str],
    embedding_present: tuple[bool, bool, bool],
    incomplete: str,
    metadata: str,
) -> None:
    """Both vector files, or neither, and never files without their metadata."""
    if present_vectors != _VECTOR_FILES:
        raise ValueError(incomplete)
    if not all(embedding_present):
        raise ValueError(metadata)


def _require_present_vectors(
    present_vectors: set[str], embedding_present: tuple[bool, bool, bool]
) -> None:
    if not present_vectors:
        return
    _require_vector_set(
        present_vectors,
        embedding_present,
        "partial vector artifacts are forbidden",
        "vector artifacts require embedding metadata",
    )


def _require_vector_contract(
    vector_state: object, seen: set[str], embedding_present: tuple[bool, bool, bool]
) -> str:
    if vector_state not in {"absent", "complete", "stale"}:
        raise ValueError("vector_state must be absent, complete, or stale")
    present_vectors = seen & _VECTOR_FILES
    _require_complete_vectors(vector_state, present_vectors, embedding_present)
    if vector_state == "absent" and (present_vectors or any(embedding_present)):
        raise ValueError("absent vectors must not have vector artifacts or metadata")
    _require_present_vectors(present_vectors, embedding_present)
    return str(vector_state)


def _require_complete_vectors(
    vector_state: object, present_vectors: set[str], embedding_present: tuple[bool, bool, bool]
) -> None:
    if vector_state != "complete":
        return
    _require_vector_set(
        present_vectors,
        embedding_present,
        "complete vectors require vectors.npy and vectors.json",
        "complete vectors require embedding metadata",
    )


def _require_graph_v3_contract(
    normalized: dict[str, object], graph_schema: str | None
) -> None:
    if graph_schema != "evidence-graph/v3":
        return
    _require_v3_manifest(normalized)


def _require_v3_manifest(normalized: dict[str, object]) -> None:
    if normalized["schema_version"] == "corpus-generation/v1":
        raise ValueError("evidence-graph/v3 requires corpus-generation/v2")
    if "code_capture" not in normalized:
        raise ValueError("evidence-graph/v3 requires code_capture")


def _require_schema_contract(
    normalized: dict[str, object], graph_schema: str | None, seen: set[str]
) -> None:
    _require_graph_v3_contract(normalized, graph_schema)
    if normalized["schema_version"] != "corpus-generation/v2":
        return
    _require_v2_contract(normalized, graph_schema, seen)


def _require_v2_artifacts(seen: set[str]) -> None:
    allowed = _V2_REQUIRED_ARTIFACTS | _V2_OPTIONAL_ARTIFACTS
    if not _V2_REQUIRED_ARTIFACTS <= seen or not seen <= allowed:
        raise ValueError("corpus-generation/v2 has an invalid artifact contract")


def _require_v2_contract(
    normalized: dict[str, object], graph_schema: str | None, seen: set[str]
) -> None:
    _require_v2_artifacts(seen)
    if graph_schema not in {"evidence-graph/v2", "evidence-graph/v3"}:
        raise ValueError("corpus-generation/v2 requires the Evidence Graph schema")
    if "repository_scope" not in normalized:
        raise ValueError("corpus-generation/v2 requires a validated repository scope")


def _validate_artifact_databases(
    generation_path: Path,
    normalized: dict[str, object],
    graph_schema: str | None,
    state_root: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    from evidence_graph import validate_generation_artifact

    if normalized["schema_version"] == "corpus-generation/v2":
        from search_memory import validate_generation_fts_artifact

        validate_generation_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        validate_generation_fts_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            cancelled=cancelled,
        )
        return
    if graph_schema in {"evidence-graph/v2", "evidence-graph/v3"}:
        validate_generation_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )


def _source_membership(source_manifest: dict, source_sizes: dict) -> list[tuple]:
    return [
        (
            source["logical_id"],
            source["relative_path"],
            source["sha256"],
            source_sizes.get(source["logical_id"]),
        )
        for source in source_manifest["sources"]
    ]


def _captured_membership(normalized: dict[str, object]) -> list[tuple]:
    return [
        (item["source_id"], item["relative_path"], item["sha256"], item["stat"]["size"])
        for item in normalized["code_capture"]["files"]
    ]


def _stored_source_sizes(database_path: Path) -> dict[str, int]:
    with closing(
        sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=0)
    ) as database:
        return {
            row[0]: row[1]
            for row in database.execute(
                "SELECT source_id, size FROM source ORDER BY source_id"
            ).fetchall()
        }


def _validate_code_capture_membership(
    generation_path: Path, normalized: dict[str, object], state_root: Path
) -> None:
    import corpus_snapshot
    import evidence_graph

    raw = read_runtime_bytes(
        generation_path / "source-manifest.json",
        state_root,
        max_bytes=evidence_graph.MAX_SOURCE_MANIFEST_BYTES,
    )
    try:
        source_manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source manifest must contain valid UTF-8 JSON") from exc
    source_manifest = corpus_snapshot.validate_canonical_source_manifest(source_manifest)
    sizes = _stored_source_sizes(generation_path / "evidence.sqlite3")
    if _captured_membership(normalized) != _source_membership(source_manifest, sizes):
        raise ValueError("code_capture files must match canonical source membership")


def _normalized_manifest(
    value: dict[str, object], raw: bytes, expected_id: str
) -> tuple[dict[str, object], tuple[bool, bool, bool], str | None]:
    """The manifest as the catalog stores it, with what the contracts need."""
    _require_manifest_shape(value, raw, expected_id)
    parent = _manifest_parent(value, expected_id)
    normalized = _normalized_versions(value, expected_id)
    normalized["tokenizer_config_sha256"] = _required_sha256(
        value["tokenizer_config_sha256"], "tokenizer_config_sha256"
    )
    embedding_id, embedding_revision, dimensions, present = _embedding_fields(value)
    normalized["embedding_model_id"] = embedding_id
    normalized["embedding_model_revision"] = embedding_revision
    normalized["vector_dimensions"] = dimensions
    graph_schema, graph_extractor = _graph_fields(value)
    normalized["graph_schema_version"] = graph_schema
    normalized["graph_extractor_version"] = graph_extractor
    _apply_optional_sections(normalized, value, parent)
    normalized["source_manifest_sha256"] = _required_sha256(
        value["source_manifest_sha256"], "source_manifest_sha256"
    )
    return normalized, present, graph_schema


def _require_stable_scan(
    generation_path: Path,
    initial_seal: tuple[_EntrySeal, ...],
    initial_files: set[str],
    seen: set[str],
    **stop: object,
) -> tuple[_EntrySeal, ...]:
    final_seal, actual_files = _scan_generation(generation_path, **stop)
    if final_seal != initial_seal:
        raise PermissionError("generation changed during validation")
    if initial_files != actual_files or actual_files != seen | {"manifest.json"}:
        raise ValueError("manifest must bind every generation artifact")
    return final_seal


# Semantic validation of a generation is a pure function of its bytes: the
# sources it is checked against come from the generation's own source manifest
# and evidence database, never from the live vault, and every call hashes every
# artifact against the manifest before the semantic check runs. The same bytes
# therefore cannot produce a different verdict — while on this vault one check
# cost 1.95 s and one query ran five of them, which put the 10-second MCP budget
# out of reach. See docs/research/2026-08-24-verify-the-same-bytes-once.md.
_MAX_REMEMBERED_VALIDATIONS = 8
_VALIDATED_GENERATIONS: OrderedDict[tuple, bool] = OrderedDict()
_VALIDATION_MEMORY_LOCK = threading.Lock()


def _validation_key(
    generation_path: Path,
    generation_id: str,
    manifest_digest: str,
    digests: dict[str, str],
) -> tuple:
    """Identity earned by hashing, not assumed: id, manifest, every artifact.

    The path is part of it so two vaults that happen to hold identical bytes are
    still each answered for themselves.
    """
    return (
        str(generation_path),
        generation_id,
        manifest_digest,
        tuple(sorted(digests.items())),
    )


def _already_validated(key: tuple) -> bool:
    with _VALIDATION_MEMORY_LOCK:
        if key not in _VALIDATED_GENERATIONS:
            return False
        _VALIDATED_GENERATIONS.move_to_end(key)
        return True


def _remember_validated(key: tuple) -> None:
    with _VALIDATION_MEMORY_LOCK:
        _VALIDATED_GENERATIONS[key] = True
        while len(_VALIDATED_GENERATIONS) > _MAX_REMEMBERED_VALIDATIONS:
            _VALIDATED_GENERATIONS.popitem(last=False)


def _validate_databases_once(
    key: tuple,
    generation_path: Path,
    normalized: dict[str, object],
    graph_schema: str | None,
    state_root: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    """The expensive half, skipped only for bytes already proven identical."""
    if _already_validated(key):
        return
    _validate_artifact_databases(
        generation_path,
        normalized,
        graph_schema,
        state_root,
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
    )
    if "code_capture" in normalized:
        _validate_code_capture_membership(generation_path, normalized, state_root)
    _remember_validated(key)


def _validate_generation(
    generation_path: Path,
    state_root: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[str, object], tuple[_EntrySeal, ...]]:
    _check_cancelled(cancelled)
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    stop = {"deadline": deadline, "monotonic": monotonic, "cancelled": cancelled}
    _check_deadline(deadline, monotonic)
    _validate_directory(generation_path, state_root)
    initial_seal, initial_files = _scan_generation(generation_path, **stop)
    expected_id = _generation_id(generation_path.name)
    _check_deadline(deadline, monotonic)
    raw = read_runtime_bytes(
        generation_path / "manifest.json", state_root, max_bytes=MAX_MANIFEST_BYTES
    )
    _check_deadline(deadline, monotonic)
    value = _manifest_object(raw)
    normalized, embedding_present, graph_schema = _normalized_manifest(
        value, raw, expected_id
    )
    scan = _scan_artifacts(value["artifacts"], generation_path, state_root, **stop)
    normalized["artifacts"] = scan.normalized
    normalized["vector_state"] = _require_vector_contract(
        value["vector_state"], scan.seen, embedding_present
    )
    _require_schema_contract(normalized, graph_schema, scan.seen)
    _validate_databases_once(
        _validation_key(generation_path, expected_id, sha256_bytes(raw), scan.digests),
        generation_path,
        normalized,
        graph_schema,
        state_root,
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
    )
    final_seal = _require_stable_scan(
        generation_path, initial_seal, initial_files, scan.seen, **stop
    )
    _check_deadline(deadline, monotonic)
    digests = {"manifest.json": sha256_bytes(raw), **scan.digests}
    return normalized, _content_seal(final_seal, digests)


class GenerationCatalog:
    """Catalog generic immutable generations and atomically select one active ID."""

    def __init__(
        self,
        state_root: Path,
        *,
        catalog_path: Path | None = None,
        clock: Callable[[], datetime | str] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_catalog_bytes: int = MAX_CATALOG_BYTES,
    ) -> None:
        _require_catalog_bound(max_catalog_bytes)
        self.state_root = Path(state_root)
        self.max_catalog_bytes = max_catalog_bytes
        validate_state_root(self.state_root)
        default = self.state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        self.catalog_path = Path(catalog_path) if catalog_path is not None else default
        _require_inside_state_root(
            self.catalog_path.resolve(strict=False), self.state_root.resolve(strict=True)
        )
        self.generations_path = self.catalog_path.parent / "generations"
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.generations_path.mkdir(parents=True, exist_ok=True)
        fsync_directory(self.catalog_path.parent)
        fsync_directory(self.catalog_path.parent.parent)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._candidate_issuer = object()
        self._read_only = False
        with closing(self._connect()) as database, database:
            self._ensure_schema(database)

    @classmethod
    def open_existing_read_only(
        cls,
        state_root: Path,
        *,
        catalog_path: Path | None = None,
        clock: Callable[[], datetime | str] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_catalog_bytes: int = MAX_CATALOG_BYTES,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GenerationCatalog:
        """Open an existing catalog without creating or repairing runtime state."""
        _require_catalog_bound(max_catalog_bytes)
        catalog = cls.__new__(cls)
        catalog._monotonic = monotonic or time.monotonic

        def check_stop() -> None:
            _check_cancelled(cancelled)
            catalog._check_deadline(deadline)

        check_stop()
        catalog.state_root = Path(state_root)
        check_stop()
        resolved_state_root = catalog.state_root.resolve(strict=True)
        check_stop()
        catalog.max_catalog_bytes = max_catalog_bytes
        default = catalog.state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        catalog.catalog_path = Path(catalog_path) if catalog_path is not None else default
        check_stop()
        _require_inside_state_root(
            catalog.catalog_path.resolve(strict=True), resolved_state_root
        )
        check_stop()
        catalog.generations_path = catalog.catalog_path.parent / "generations"
        catalog._clock = clock or (lambda: datetime.now(timezone.utc))
        catalog._candidate_issuer = object()
        catalog._read_only = True
        check_stop()
        with closing(catalog._readonly()):
            check_stop()
        check_stop()
        return catalog

    def _connect(self, *, deadline: float | None = None) -> sqlite3.Connection:
        self._check_deadline(deadline)
        if self.catalog_path.exists() or self.catalog_path.is_symlink():
            validate_runtime_file(
                self.catalog_path,
                self.state_root,
                max_bytes=self.max_catalog_bytes,
            )
        return open_operational_db(self.catalog_path, busy_ms=self._busy_ms(deadline))

    def _busy_ms(self, deadline: float | None) -> int:
        """Readers wait for a lock exactly as writers do; busy is not an error."""
        if deadline is None:
            return UNBOUNDED_BUSY_MS
        return self._remaining_busy_ms(deadline)

    def _check_deadline(self, deadline: float | None) -> None:
        _require_absolute_deadline(deadline)
        _check_deadline(deadline, self._monotonic)

    def _remaining_busy_ms(self, deadline: float) -> int:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("generation catalog deadline reached")
        return min(BUSY_MS, max(0, int(remaining * 1000)))

    @contextmanager
    def _write_transaction(self, deadline: float | None):
        self._check_deadline(deadline)
        if self._read_only:
            raise PermissionError("read-only generation catalog cannot write")
        with closing(self._connect(deadline=deadline)) as database:
            try:
                self._apply_busy_timeout(database, deadline)
                database.execute("BEGIN IMMEDIATE")
                self._check_deadline(deadline)
                yield database
                self._apply_busy_timeout(database, deadline)
                database.commit()
            except sqlite3.OperationalError as exc:
                database.rollback()
                _raise_writer_timeout(exc, deadline)
                raise
            except BaseException:
                database.rollback()
                raise

    def _apply_busy_timeout(self, database: sqlite3.Connection, deadline: float | None) -> None:
        if deadline is None:
            return
        database.execute(f"PRAGMA busy_timeout={self._remaining_busy_ms(deadline):d}")

    def _readonly(self, *, deadline: float | None = None) -> sqlite3.Connection:
        return open_readonly_operational_db(
            self.catalog_path,
            self.state_root,
            max_bytes=self.max_catalog_bytes,
            busy_ms=self._busy_ms(deadline),
        )

    @staticmethod
    def _ensure_schema(database: sqlite3.Connection) -> None:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS generations (
                generation_id TEXT PRIMARY KEY,
                parent_generation_id TEXT,
                manifest_json BLOB NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                active_generation_id TEXT
            );
            INSERT OR IGNORE INTO catalog_state(singleton, active_generation_id)
            VALUES (1, NULL);
            CREATE TABLE IF NOT EXISTS activation_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_id TEXT NOT NULL,
                activated_at TEXT NOT NULL
            );
            """
        )

    def _validate(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], bytes, tuple[_EntrySeal, ...]]:
        identifier = _generation_id(generation_id)
        manifest, seal = _validate_generation(
            self.generations_path / identifier,
            state_root=self.state_root,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        return manifest, canonical_json_bytes(manifest), seal

    def _catalog_identity(self) -> _EntrySeal:
        validate_runtime_file(
            self.catalog_path,
            self.state_root,
            max_bytes=self.max_catalog_bytes,
        )
        return _entry_seal(self.catalog_path, "catalog.sqlite3")

    @staticmethod
    def _same_catalog_identity(left: _EntrySeal, right: _EntrySeal) -> bool:
        return (
            left.path,
            left.kind,
            left.device,
            left.inode,
            left.mode,
        ) == (
            right.path,
            right.kind,
            right.device,
            right.inode,
            right.mode,
        )

    def _validate_candidate(
        self,
        generation_id: str,
        *,
        expected_repository_scope: RepositoryScope | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> _ValidatedCandidate:
        """Fully validate and mint one process-local publication capability."""
        if expected_repository_scope is not None and not isinstance(
            expected_repository_scope, RepositoryScope
        ):
            raise TypeError("expected_repository_scope must be a RepositoryScope or None")
        identifier = _generation_id(generation_id)
        manifest, encoded, seal = self._validate(
            identifier, deadline=deadline, cancelled=cancelled
        )
        scope = _manifest_scope(manifest)
        _require_publication_scope(scope, expected_repository_scope)
        scope_bytes = None if scope is None else canonical_json_bytes(scope.as_dict())
        return _ValidatedCandidate(
            issuer=self._candidate_issuer,
            generation_id=identifier,
            generation_path=(self.generations_path / identifier).resolve(strict=True),
            state_root=self.state_root.resolve(strict=True),
            catalog_path=self.catalog_path.resolve(strict=True),
            catalog_identity=self._catalog_identity(),
            manifest_bytes=encoded,
            manifest_sha256=sha256_bytes(encoded),
            repository_scope_bytes=scope_bytes,
            seal=seal,
            mint=_CANDIDATE_MINT,
        )

    def _candidate_payload(
        self,
        candidate: object,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], bytes, _GenerationSealCapability]:
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        if not isinstance(candidate, _ValidatedCandidate) or (
            candidate.issuer is not self._candidate_issuer
        ):
            raise TypeError("validated candidate was not issued by this catalog")
        generation_path = self.generations_path / candidate.generation_id
        self._require_candidate_identity(candidate, generation_path)
        manifest = _candidate_manifest(candidate)
        capability = self._acquire_seal_capability(
            generation_path,
            candidate.seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        return manifest, candidate.manifest_bytes, capability

    def _require_candidate_identity(self, candidate, generation_path: Path) -> None:
        same_paths = (
            generation_path.resolve(strict=True) == candidate.generation_path
            and self.state_root.resolve(strict=True) == candidate.state_root
            and self.catalog_path.resolve(strict=True) == candidate.catalog_path
        )
        same_catalog = self._same_catalog_identity(
            candidate.catalog_identity, self._catalog_identity()
        )
        if not same_paths or not same_catalog:
            raise PermissionError("validated candidate catalog or path identity changed")

    def _write_registration(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        generation_id: str,
        parent_generation_id: object,
        encoded: bytes,
        digest: str,
        timestamp: str,
        deadline: float | None,
    ) -> None:
        """One registration row, written once and immutable afterwards."""
        row = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        _require_registration_match(row, encoded, digest)
        self._require_registration_capacity(database, row)
        if not capability.revalidate():
            raise ValueError("generation changed before registration")
        self._insert_registration(
            database,
            row,
            generation_id=generation_id,
            parent_generation_id=parent_generation_id,
            encoded=encoded,
            digest=digest,
            timestamp=timestamp,
        )
        self._check_deadline(deadline)

    def _require_registration_capacity(self, database: sqlite3.Connection, row) -> None:
        if row is None:
            self._require_capacity(database, "generations", MAX_GENERATIONS, "generation")

    def _insert_registration(
        self,
        database: sqlite3.Connection,
        row,
        *,
        generation_id: str,
        parent_generation_id: object,
        encoded: bytes,
        digest: str,
        timestamp: str,
    ) -> None:
        """An existing row is the idempotent retry; it is never rewritten."""
        if row is not None:
            return
        database.execute(
            "INSERT INTO generations "
            "(generation_id, parent_generation_id, manifest_json, "
            "manifest_sha256, registered_at) VALUES (?, ?, ?, ?, ?)",
            (generation_id, parent_generation_id, encoded, digest, timestamp),
        )
        self._require_catalog_bytes(database)

    def _register_validated(
        self,
        candidate: object,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Register a same-process candidate without repeating semantic validation."""
        manifest, encoded, capability = self._candidate_payload(
            candidate, deadline=deadline, cancelled=cancelled
        )
        assert isinstance(candidate, _ValidatedCandidate)
        timestamp = _utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                _check_cancelled(cancelled)
                self._write_registration(
                    database,
                    capability,
                    generation_id=candidate.generation_id,
                    parent_generation_id=manifest.get("parent_generation_id"),
                    encoded=encoded,
                    digest=candidate.manifest_sha256,
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return manifest

    def register(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Register a complete generation; identical retries are idempotent."""
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        manifest, encoded, seal = self._validate(
            generation_id, deadline=deadline, cancelled=cancelled
        )
        timestamp = _utc_timestamp(self._clock)
        capability = self._acquire_seal_capability(
            self.generations_path / generation_id,
            seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        with capability:
            with self._write_transaction(deadline) as database:
                _check_cancelled(cancelled)
                self._write_registration(
                    database,
                    capability,
                    generation_id=generation_id,
                    parent_generation_id=manifest.get("parent_generation_id"),
                    encoded=encoded,
                    digest=sha256_bytes(encoded),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return manifest

    def _registered_generation(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], tuple[_EntrySeal, ...]]:
        _check_cancelled(cancelled)
        manifest, encoded, seal = self._validate(
            generation_id, deadline=deadline, cancelled=cancelled
        )
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            row = database.execute(
                "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        self._check_deadline(deadline)
        if row is None:
            raise ValueError("generation is not registered")
        if bytes(row["manifest_json"]) != encoded or row["manifest_sha256"] != sha256_bytes(
            encoded
        ):
            raise ValueError("registered generation no longer matches its manifest")
        return manifest, seal

    def _registered_manifest(
        self, generation_id: str, *, deadline: float | None = None
    ) -> dict[str, object]:
        return self._registered_generation(generation_id, deadline=deadline)[0]

    def _deadline_seal_unchanged(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        deadline: float | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        options: dict[str, object] = {}
        if deadline is not None:
            options["deadline"] = deadline
        if cancelled is not None:
            options["cancelled"] = cancelled
        return self._seal_unchanged(generation_path, expected, **options)

    def _require_expected_tree(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, _EntrySeal]:
        """The files the seal names, once the tree on disk still matches it."""
        current, files = _scan_generation(
            generation_path,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        expected_files = _expected_file_entries(expected)
        if current != _metadata_only(expected) or files != set(expected_files):
            raise PermissionError("generation changed before publication sealing")
        return expected_files

    def _hold_expected_files(
        self,
        generation_path: Path,
        expected_files: dict[str, _EntrySeal],
        held_files: list,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        """Open every named file, appending as we go so the caller can close them."""
        for relative, entry in expected_files.items():
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
            held_files.append(self._held_file_for(generation_path, relative, entry))

    def _held_file_for(self, generation_path: Path, relative: str, entry) -> _HeldFile:
        path = generation_path.joinpath(*relative.split("/"))
        if _entry_seal(path, relative) != replace(entry, sha256=None):
            raise PermissionError("generation member changed before opening")
        descriptor = _open_read_descriptor(path)
        try:
            opened = os.fstat(descriptor)
            if not _same_regular_file(opened, path):
                raise PermissionError("generation member identity changed while opening")
            return _HeldFile(
                path=path,
                relative=relative,
                descriptor=descriptor,
                opened_state=_stable_descriptor_state(descriptor),
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _require_sealed_content(self, capability, expected_files: dict) -> None:
        for held in capability.held_files:
            entry = expected_files[held.relative]
            size, digest = _hash_descriptor(
                held.descriptor,
                entry.size,
                deadline=capability.deadline,
                monotonic=self._monotonic,
                cancelled=capability.cancelled,
            )
            if size != entry.size or digest != entry.sha256:
                raise PermissionError("generation content changed while sealing")

    def _acquire_seal_capability(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> _GenerationSealCapability:
        """Hash a bounded generation through held descriptors outside the writer lock."""
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        generation_path = Path(generation_path)
        expected_files = self._require_expected_tree(
            generation_path, expected, deadline=deadline, cancelled=cancelled
        )
        held_files: list[_HeldFile] = []
        try:
            self._hold_expected_files(
                generation_path,
                expected_files,
                held_files,
                deadline=deadline,
                cancelled=cancelled,
            )
            capability = _GenerationSealCapability(
                generation_path,
                expected,
                tuple(held_files),
                deadline=deadline,
                monotonic=self._monotonic,
                cancelled=cancelled,
            )
            self._require_sealed_content(capability, expected_files)
            if not capability.revalidate():
                raise PermissionError("generation changed during publication sealing")
            return capability
        except BaseException:
            _close_descriptors(held_files)
            raise

    def _seal_unchanged(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Recheck bounded metadata and content without a catalog writer lock."""
        try:
            capability = self._acquire_seal_capability(
                generation_path,
                expected,
                deadline=deadline,
                cancelled=cancelled,
            )
        except (FileNotFoundError, PermissionError, ValueError):
            return False
        with capability:
            return capability.revalidate()

    @staticmethod
    def _require_capacity(database: sqlite3.Connection, table: str, limit: int, label: str) -> None:
        if limit < 1:
            raise ValueError(f"{label} row ceiling must be positive")
        count = database.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT ?)",
            (limit + 1,),
        ).fetchone()[0]
        if count >= limit:
            raise ValueError(f"{label} row ceiling reached")

    @staticmethod
    def _bounded_rows(
        database: sqlite3.Connection,
        query: str,
        limit: int,
        label: str,
    ) -> list[sqlite3.Row]:
        rows = list(database.execute(query, (limit + 1,)))
        if len(rows) > limit:
            raise ValueError(f"{label} row ceiling exceeded")
        return rows

    def _require_catalog_bytes(self, database: sqlite3.Connection) -> None:
        page_count = database.execute("PRAGMA main.page_count").fetchone()[0]
        page_size = database.execute("PRAGMA main.page_size").fetchone()[0]
        if not _valid_page_metric(page_count, 0) or not _valid_page_metric(page_size, 1):
            raise ValueError("catalog page metadata is invalid")
        # Reserve one page because commit may finalize a pending b-tree allocation.
        if page_count * page_size + page_size > self.max_catalog_bytes:
            raise ValueError("catalog byte ceiling would be exceeded")

    def _require_activation_ready(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        identifier: str,
        expected_active: str | None,
        registration_token: tuple[bytes, str],
    ) -> None:
        registered = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (identifier,),
        ).fetchone()
        _require_registration_token(registered, registration_token)
        if identifier != expected_active:
            self._require_capacity(
                database, "activation_history", MAX_ACTIVATION_HISTORY, "history"
            )
        if not capability.revalidate():
            raise ValueError("generation changed before activation")

    def _cas_activate(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        identifier: str,
        expected_active: str | None,
        registration_token: tuple[bytes, str],
        timestamp: str,
        deadline: float | None,
    ) -> bool | None:
        """None when the pointer moved under us; otherwise whether it now points here."""
        if not _pointer_is(database, expected_active):
            self._check_deadline(deadline)
            return None
        self._require_activation_ready(
            database,
            capability,
            identifier=identifier,
            expected_active=expected_active,
            registration_token=registration_token,
        )
        self._check_deadline(deadline)
        if identifier != expected_active:
            self._move_active_pointer(database, identifier, timestamp)
        self._check_deadline(deadline)
        return True

    def _move_active_pointer(
        self, database: sqlite3.Connection, identifier: str, timestamp: str
    ) -> None:
        database.execute(
            "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
            (identifier,),
        )
        database.execute(
            "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
            (identifier, timestamp),
        )
        self._require_catalog_bytes(database)

    def _activate_validated(
        self,
        candidate: object,
        *,
        expected_active: str | None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """CAS-activate a same-process candidate without semantic revalidation."""
        manifest, encoded, capability = self._candidate_payload(
            candidate, deadline=deadline, cancelled=cancelled
        )
        assert isinstance(candidate, _ValidatedCandidate)
        if expected_active is not None:
            expected_active = _generation_id(expected_active)
        timestamp = _utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                _check_cancelled(cancelled)
                outcome = self._cas_activate(
                    database,
                    capability,
                    identifier=candidate.generation_id,
                    expected_active=expected_active,
                    registration_token=(encoded, candidate.manifest_sha256),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return bool(outcome)

    def activate(
        self,
        generation_id: str,
        *,
        expected_active: str | None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Atomically activate a validated generation if the pointer matches."""
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        identifier = _generation_id(generation_id)
        if expected_active is not None:
            expected_active = _generation_id(expected_active)
        manifest, seal = self._registered_generation(
            identifier, deadline=deadline, cancelled=cancelled
        )
        encoded = canonical_json_bytes(manifest)
        capability = self._acquire_seal_capability(
            self.generations_path / identifier,
            seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        timestamp = _utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                _check_cancelled(cancelled)
                outcome = self._cas_activate(
                    database,
                    capability,
                    identifier=identifier,
                    expected_active=expected_active,
                    registration_token=(encoded, sha256_bytes(encoded)),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return bool(outcome)

    def _drop_registration(self, identifier: str) -> bool | None:
        """Remove the row; None means the generation is in use and must be kept."""
        cleanup_deadline = self._monotonic() + CLEANUP_CATALOG_FENCE_SECONDS
        with self._write_transaction(cleanup_deadline) as database:
            if _generation_in_use(database, identifier):
                return None
            return self._delete_registration(database, identifier)

    @staticmethod
    def _delete_registration(database: sqlite3.Connection, identifier: str) -> bool:
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id = ?", (identifier,)
        ).fetchone()
        if registered is None:
            return False
        database.execute("DELETE FROM generations WHERE generation_id = ?", (identifier,))
        return True

    def _remove_generation_tree(
        self,
        identifier: str,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        generation_path = self.generations_path / identifier
        if not generation_path.exists() and not generation_path.is_symlink():
            return False
        _scan_generation(
            generation_path,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        shutil.rmtree(generation_path)
        if generation_path.exists():
            raise OSError("generation cleanup did not remove candidate")
        fsync_directory(self.generations_path)
        return True

    def discard_unactivated(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Remove a never-activated registration after fenced publication aborts."""
        identifier = _generation_id(generation_id)
        _require_absolute_deadline(deadline)
        removed_registration = self._drop_registration(identifier)
        if removed_registration is None:
            return False

        # The committed delete cannot be rolled back by filesystem failures. A
        # second writer fence keeps activation and registration from racing the
        # recursive cleanup.
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        with self._write_transaction(deadline) as database:
            _check_cancelled(cancelled)
            if _still_referenced(database, identifier):
                return False
            removed_directory = self._remove_generation_tree(
                identifier, deadline=deadline, cancelled=cancelled
            )
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
        return removed_registration or removed_directory

    def retained_generations(
        self,
        *,
        retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """The generations retention protects: the active one and its ancestors."""
        _require_retained_ancestors(retained_ancestors)
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            return _retained_generations(database, retained_ancestors)

    def registered_generation_ids(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """Every registered generation id, without validating a single tree."""
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._bounded_rows(
                database,
                "SELECT generation_id FROM generations LIMIT ?",
                MAX_GENERATIONS,
                "generation",
            )
        return tuple(row["generation_id"] for row in rows)

    def activated_generation_ids(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> frozenset[str]:
        """Every generation that has ever been active — the superseded set."""
        self._check_deadline(deadline)
        _check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._bounded_rows(
                database,
                "SELECT DISTINCT generation_id FROM activation_history LIMIT ?",
                MAX_ACTIVATION_HISTORY,
                "history",
            )
        return frozenset(row["generation_id"] for row in rows)

    def _require_paired_generation(
        self, database: sqlite3.Connection, identifier: str
    ) -> None:
        """A row with no tree, or a tree with no row, is a half-finished
        operation. Report it; never guess which half was meant."""
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id = ?", (identifier,)
        ).fetchone()
        if registered is None:
            raise ValueError("generation is not registered")
        if not (self.generations_path / identifier).is_dir():
            raise ValueError("generation tree is missing")

    def _require_prunable(
        self, database: sqlite3.Connection, identifier: str, retained: tuple[str, ...]
    ) -> None:
        if identifier in retained:
            raise ValueError("retained generation cannot be discarded")
        self._require_paired_generation(database, identifier)
        _require_activated(database, identifier)

    @staticmethod
    def _delete_generation_rows(database: sqlite3.Connection, identifier: str) -> None:
        """Registration and activation history leave together: a surviving
        history row keeps `_generation_in_use` refusing a tree that is gone."""
        database.execute("DELETE FROM generations WHERE generation_id = ?", (identifier,))
        database.execute(
            "DELETE FROM activation_history WHERE generation_id = ?", (identifier,)
        )

    def discard_superseded(
        self,
        generation_id: str,
        *,
        retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Drop one generation retention no longer keeps: row, history and tree.

        `discard_unactivated` refuses anything that was ever activated, which is
        every generation but one on a vault that has been building nightly. This
        removes a superseded one, and it removes all three parts inside a single
        write transaction: a filesystem failure rolls the registration back
        rather than stranding a row whose tree is gone. The live set is
        recomputed from that same transaction, so an activation racing this call
        cannot make it delete what has just become live.
        """
        identifier = _generation_id(generation_id)
        _require_absolute_deadline(deadline)
        _require_retained_ancestors(retained_ancestors)
        with self._write_transaction(deadline) as database:
            _check_cancelled(cancelled)
            retained = _retained_generations(database, retained_ancestors)
            _require_active_root(retained)
            self._require_prunable(database, identifier, retained)
            self._delete_generation_rows(database, identifier)
            removed = self._remove_generation_tree(
                identifier, deadline=deadline, cancelled=cancelled
            )
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
        return removed

    def _snapshot_catalog(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str | None, list[str], dict[str, str | None]]:
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            state = database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()
            if state is None:
                raise ValueError("catalog active pointer is missing")
            history_rows = self._bounded_rows(
                database,
                "SELECT generation_id FROM activation_history ORDER BY sequence DESC LIMIT ?",
                MAX_ACTIVATION_HISTORY,
                "history",
            )
            history = [row["generation_id"] for row in history_rows]
            generation_rows = self._bounded_rows(
                database,
                "SELECT generation_id, parent_generation_id FROM generations LIMIT ?",
                MAX_GENERATIONS,
                "generation",
            )
            parents = {row["generation_id"]: row["parent_generation_id"] for row in generation_rows}
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        return state["active_generation_id"], history, parents

    @staticmethod
    def _fallback_order(
        active: str | None,
        history: list[str],
        parents: dict[str, str | None],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        prior = [identifier for identifier in [active, *history] if identifier is not None]
        for identifier in prior:
            _check_cancelled(cancelled)
            _check_deadline(deadline, time.monotonic)
            _append_unseen(ordered, seen, identifier)
        for identifier in prior:
            _check_cancelled(cancelled)
            _check_deadline(deadline, time.monotonic)
            _append_ancestors(ordered, seen, parents, identifier, deadline, cancelled)
        return ordered

    def registered_manifests(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[tuple[str, str, dict[str, object]]]:
        """Every registered generation as (id, registered_at, manifest), newest first.

        Only manifests whose stored bytes still hash to the recorded digest and
        still round-trip through canonical JSON are returned; a row that fails
        either is skipped rather than raised, because one damaged registration
        must not hide every healthy one from a listing.
        """
        rows = self._registered_rows(deadline)
        manifests: list[tuple[str, str, dict[str, object]]] = []
        for row in rows:
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
            manifest = _verified_manifest(row)
            if manifest is not None:
                manifests.append((row["generation_id"], row["registered_at"], manifest))
        return manifests

    def _registered_rows(self, deadline: float | None) -> list[sqlite3.Row]:
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            return self._bounded_rows(
                database,
                "SELECT generation_id, registered_at, manifest_json, manifest_sha256 "
                "FROM generations ORDER BY registered_at DESC, generation_id DESC "
                "LIMIT ?",
                MAX_GENERATIONS,
                "generation",
            )

    def _scoped_generation(
        self,
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """The newest registered generation of a repository the pointer does not name.

        This is the whole of CODE-03's read side, and it is deliberately
        asymmetric to `_select_fallback`: it never moves the active pointer,
        never writes activation history and never repairs anything. The single
        pointer belongs to the vault. Were a foreign repository allowed to
        activate, indexing one would make the vault's own scope unresolvable and
        every knowledge query would fall back to the legacy index -- NEW-65,
        recreated deliberately.
        """
        if repository_scope is None:
            return None
        for identifier, _registered_at, manifest in self.registered_manifests(
            deadline=deadline, cancelled=cancelled
        ):
            if not _manifest_belongs_to(manifest, repository_scope):
                continue
            selected = self._validated_scoped_generation(
                identifier, deadline, cancelled
            )
            if selected is not None:
                return selected
        return None

    def _validated_scoped_generation(
        self,
        identifier: str,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """Re-validate artifacts before handing a generation out; None when unusable."""
        try:
            selected, _seal = self._registered_generation_shaped(
                identifier, deadline, cancelled
            )
        except (FileNotFoundError, PermissionError, TypeError, ValueError):
            return None
        return selected

    def _registered_repository_scope(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RepositoryScope | None:
        """Read repository eligibility from the catalog without validating artifacts."""
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            row = database.execute(
                "SELECT manifest_json, manifest_sha256 FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        _check_cancelled(cancelled)
        self._check_deadline(deadline)
        if row is None:
            return None
        encoded = bytes(row["manifest_json"])
        if sha256_bytes(encoded) != row["manifest_sha256"]:
            return None
        try:
            manifest = json.loads(encoded)
            if canonical_json_bytes(manifest) != encoded:
                return None
            return RepositoryScope.from_dict(manifest.get("repository_scope"))
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _scope_admits(
        self,
        identifier: str | None,
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        """Whether a registered generation belongs to this repository checkout.

        Eligibility is the repository identity, not the commit the generation
        happened to be built at. Comparing whole scopes made every generation
        ineligible after the next commit, so retrieval fell back to the legacy
        index almost always on a vault that commits its own runtime (NEW-65). The
        commit stays in the manifest as provenance, and publication still binds it
        exactly.
        """
        if identifier is None or repository_scope is None:
            return True
        registered = self._registered_repository_scope(
            identifier, deadline=deadline, cancelled=cancelled
        )
        if registered is None:
            return False
        return registered.same_repository(repository_scope)

    def _registered_generation_shaped(
        self,
        identifier: str,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], tuple[_EntrySeal, ...]]:
        """Keep the bare call shape when nothing bounds the read.

        A caller may replace this method with a positional stub; passing unused
        keyword arguments would turn its failure into a TypeError that fallback
        selection swallows.
        """
        if deadline is None and cancelled is None:
            return self._registered_generation(identifier)
        return self._registered_generation(
            identifier, deadline=deadline, cancelled=cancelled
        )

    def _select_fallback(
        self,
        active: str | None,
        history: list[str],
        parents: dict[str, str | None],
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object] | None, str | None, tuple[_EntrySeal, ...] | None]:
        for identifier in self._fallback_order(
            active, history, parents, deadline=deadline, cancelled=cancelled
        ):
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
            if not self._scope_admits(
                identifier, repository_scope, deadline=deadline, cancelled=cancelled
            ):
                continue
            try:
                selected, seal = self._registered_generation_shaped(
                    identifier, deadline, cancelled
                )
            except (FileNotFoundError, PermissionError, TypeError, ValueError):
                continue
            return selected, identifier, seal
        return None, None, None

    def _require_fallback_seal(
        self,
        selected_id: str | None,
        selected_seal: tuple[_EntrySeal, ...] | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if selected_id is None:
            return
        unchanged = selected_seal is not None and self._deadline_seal_unchanged(
            self.generations_path / selected_id, selected_seal, deadline, cancelled=cancelled
        )
        if not unchanged:
            raise ValueError("fallback generation changed after validation seal")

    def _repair_pointer(
        self,
        database: sqlite3.Connection,
        *,
        active: str | None,
        selected_id: str | None,
        selected_token: tuple[bytes, str] | None,
        timestamp: str,
    ) -> bool:
        """False when the pointer moved under us and the attempt must restart."""
        if not _pointer_matches(database, active):
            return False
        self._require_repair_target(database, selected_id, selected_token)
        database.execute(
            "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
            (selected_id,),
        )
        _record_activation(database, selected_id, timestamp)
        self._require_catalog_bytes(database)
        return True

    def _require_repair_target(
        self,
        database: sqlite3.Connection,
        selected_id: str | None,
        selected_token: tuple[bytes, str] | None,
    ) -> None:
        """Clearing the pointer has no target to check; repointing it does."""
        if selected_id is None:
            return
        self._require_capacity(
            database, "activation_history", MAX_ACTIVATION_HISTORY, "history"
        )
        registered = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (selected_id,),
        ).fetchone()
        _require_fallback_registration(registered, selected_token)

    def _repair_active_pointer(
        self,
        *,
        active: str | None,
        selected: dict[str, object] | None,
        selected_id: str | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        """False when the pointer moved under us and the caller must retry."""
        selected_token = _selected_token(selected, selected_id)
        timestamp = _utc_timestamp(self._clock)
        with self._write_transaction(deadline) as database:
            _check_cancelled(cancelled)
            repaired = self._repair_pointer(
                database,
                active=active,
                selected_id=selected_id,
                selected_token=selected_token,
                timestamp=timestamp,
            )
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
        return repaired

    def _active_attempt(
        self,
        *,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[bool, dict[str, object] | None]:
        """One attempt; the first element says whether the answer is settled."""
        active, history, parents = self._snapshot_catalog(
            deadline=deadline, cancelled=cancelled
        )
        if not self._scope_admits(
            active, repository_scope, deadline=deadline, cancelled=cancelled
        ):
            # CODE-03: the pointer names another repository. Answer from this
            # repository's own registered generations, without moving it.
            return True, self._scoped_generation(
                repository_scope, deadline=deadline, cancelled=cancelled
            )
        selected, selected_id, selected_seal = self._select_fallback(
            active, history, parents, repository_scope,
            deadline=deadline, cancelled=cancelled,
        )
        return self._settled_selection(
            active,
            selected,
            selected_id,
            selected_seal,
            repository_scope,
            deadline,
            cancelled,
        )

    def _or_scoped(
        self,
        selected: dict[str, object] | None,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """Fall through to this repository's own generations when nothing was found.

        `_select_fallback` walks the active pointer, the activation history and
        their parents, so a generation that was registered and never activated
        is invisible to it. That is every CODE-03 generation, by design, and it
        is also every generation of a young vault whose pointer is still empty.
        Reached only when the walk found nothing; the vault's normal case
        (pointer set, its own generation selected) never gets here.
        """
        if selected is not None:
            return selected
        return self._scoped_generation(
            repository_scope, deadline=deadline, cancelled=cancelled
        )

    def _settled_selection(
        self,
        active: str | None,
        selected: dict[str, object] | None,
        selected_id: str | None,
        selected_seal: tuple[_EntrySeal, ...] | None,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[bool, dict[str, object] | None]:
        """Settle a fallback selection, repairing the pointer when it may."""
        if selected_id == active:
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
            return True, self._or_scoped(
                selected, repository_scope, deadline, cancelled
            )
        self._require_fallback_seal(selected_id, selected_seal, deadline, cancelled)
        if self._read_only:
            return True, selected
        repaired = self._repair_active_pointer(
            active=active,
            selected=selected,
            selected_id=selected_id,
            deadline=deadline,
            cancelled=cancelled,
        )
        return repaired, selected

    def _get_active(
        self,
        *,
        repository_scope: RepositoryScope | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return a valid active generation, repairing a corrupt pointer safely."""
        for _attempt in range(3):
            _check_cancelled(cancelled)
            self._check_deadline(deadline)
            settled, manifest = self._active_attempt(
                repository_scope=repository_scope,
                deadline=deadline,
                cancelled=cancelled,
            )
            if settled:
                return manifest
        raise sqlite3.OperationalError("active generation changed during fallback repair")

    def get_active(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return a valid active generation, repairing a corrupt pointer safely."""
        return self._get_active(deadline=deadline, cancelled=cancelled)

    def get_active_for_repository(
        self,
        repository_scope: RepositoryScope,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return or repair an active generation only within one repository scope."""
        if not isinstance(repository_scope, RepositoryScope):
            raise TypeError("repository_scope must be a RepositoryScope")
        expected_scope = RepositoryScope.from_dict(repository_scope.as_dict())
        return self._get_active(
            repository_scope=expected_scope,
            deadline=deadline,
            cancelled=cancelled,
        )

    def _registered_identifiers(self, deadline: float | None) -> set[str]:
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._bounded_rows(
                database,
                "SELECT generation_id FROM generations LIMIT ?",
                MAX_GENERATIONS,
                "generation",
            )
            return {row["generation_id"] for row in rows}

    def _register_shaped(self, generation_id: str, deadline: float | None) -> None:
        """Keep the bare call shape when nothing bounds the registration."""
        if deadline is None:
            self.register(generation_id)
            return
        self.register(generation_id, deadline=deadline)

    def _recovered_child(self, entry, registered: set[str], deadline: float | None) -> bool:
        """True when this child was registered by this pass."""
        if entry.name in registered or not entry.is_dir(follow_symlinks=False):
            return False
        try:
            _generation_id(entry.name)
            if _is_link_or_reparse(Path(entry.path)):
                return False
            self._register_shaped(entry.name, deadline)
        except (FileNotFoundError, PermissionError, TypeError, ValueError):
            return False
        return True

    def recover_orphans(self, *, deadline: float | None = None) -> list[str]:
        """Register complete immediate-child generations without activating them."""
        self._check_deadline(deadline)
        registered = self._registered_identifiers(deadline)
        children = _bounded_scandir(
            self.generations_path,
            MAX_GENERATION_CHILDREN,
            "generation child count exceeds the recovery bound",
            deadline=deadline,
            monotonic=self._monotonic,
        )
        recovered: list[str] = []
        for entry in children:
            try:
                self._check_deadline(deadline)
                if self._recovered_child(entry, registered, deadline):
                    recovered.append(entry.name)
            except TimeoutError:
                if recovered:
                    return recovered
                raise
        return recovered
