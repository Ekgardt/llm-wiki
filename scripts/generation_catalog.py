"""Validated immutable generation registration and atomic activation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

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

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACTS = 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_GENERATION_BYTES = 64 * 1024 * 1024 * 1024
MAX_GENERATION_CHILDREN = 4096
MAX_CATALOG_BYTES = 256 * 1024 * 1024
MAX_GENERATIONS = 1024
MAX_ACTIVATION_HISTORY = 16384
HASH_CHUNK_BYTES = 64 * 1024
BUSY_MS = 5000

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
}
_REQUIRED_MANIFEST_KEYS = _MANIFEST_KEYS - {"parent_generation_id"}
_ARTIFACT_KEYS = {"path", "size", "sha256"}
_VECTOR_FILES = {"vectors.npy", "vectors.json"}


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


def _generation_id(value: object) -> str:
    if not isinstance(value, str) or _GENERATION_RE.fullmatch(value) is None:
        raise ValueError("generation_id must be an exact safe path component")
    if value in {".", ".."} or value[-1] in {".", " "}:
        raise ValueError("generation_id must be an exact safe path component")
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise ValueError("generation_id is a reserved path component")
    return value


def _bounded_version(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid Unicode") from exc
    return value


def _optional_version(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _bounded_version(name, value)


def _utc_timestamp(clock: Callable[[], datetime | str]) -> str:
    value = clock()
    if isinstance(value, str):
        if len(value) > 40 or any(character in value for character in "\x00\r\n"):
            raise ValueError("clock returned an invalid timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("clock must return an ISO-8601 timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("clock must return a datetime or ISO-8601 string")
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


def _bounded_scandir(
    path: Path,
    limit: int,
    message: str,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    _check_deadline(deadline, monotonic)
    with os.scandir(path) as iterator:
        for entry in iterator:
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
) -> tuple[tuple[_EntrySeal, ...], set[str]]:
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
        )
        for entry in ordered:
            _check_deadline(deadline, monotonic)
            visited += 1
            path = Path(entry.path)
            relative = path.relative_to(directory).as_posix()
            seal = _entry_seal(path, relative)
            seals.append(seal)
            if seal.kind == "directory":
                pending.append(path)
            else:
                files.add(relative)
    _check_deadline(deadline, monotonic)
    return tuple(sorted(seals)), files


def _listed_generation_files(directory: Path) -> set[str]:
    return _scan_generation(directory)[1]


def _content_seal(
    metadata_seal: tuple[_EntrySeal, ...], digests: dict[str, str]
) -> tuple[_EntrySeal, ...]:
    file_paths = {entry.path for entry in metadata_seal if entry.kind == "file"}
    if file_paths != set(digests):
        raise ValueError("content seal must bind every generation file")
    return tuple(
        replace(entry, sha256=digests[entry.path]) if entry.kind == "file" else entry
        for entry in metadata_seal
    )


def _hash_artifact(
    path: Path,
    state_root: Path,
    max_bytes: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, str]:
    _check_deadline(deadline, monotonic)
    expected = validate_runtime_file(path, state_root, max_bytes=max_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise PermissionError("artifact identity changed while opening")
        digest = hashlib.sha256()
        total = 0
        while True:
            _check_deadline(deadline, monotonic)
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            _check_deadline(deadline, monotonic)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("artifact exceeds its declared size")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not os.path.samestat(opened, after) or (opened.st_size, opened.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PermissionError("artifact changed while hashing")
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _is_link_or_reparse(path) or not os.path.samestat(after, current):
        raise PermissionError("artifact identity changed after hashing")
    _check_deadline(deadline, monotonic)
    return total, digest.hexdigest()


def _validate_generation(
    generation_path: Path,
    state_root: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, object], tuple[_EntrySeal, ...]]:
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    _check_deadline(deadline, monotonic)
    _validate_directory(generation_path, state_root)
    initial_seal, initial_files = _scan_generation(
        generation_path, deadline=deadline, monotonic=monotonic
    )
    expected_id = _generation_id(generation_path.name)
    manifest_path = generation_path / "manifest.json"
    _check_deadline(deadline, monotonic)
    raw = read_runtime_bytes(manifest_path, state_root, max_bytes=MAX_MANIFEST_BYTES)
    _check_deadline(deadline, monotonic)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest.json must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("generation manifest must be an object")
    keys = set(value)
    if not _REQUIRED_MANIFEST_KEYS <= keys or not keys <= _MANIFEST_KEYS:
        raise ValueError("generation manifest has missing or unknown properties")
    if canonical_json_bytes(value) != raw:
        raise ValueError("generation manifest must use canonical JSON")
    if _generation_id(value["generation_id"]) != expected_id:
        raise ValueError("generation_id must equal the generation directory name")

    parent = value.get("parent_generation_id")
    if parent is not None:
        parent = _generation_id(parent)
        if parent == expected_id:
            raise ValueError("generation cannot be its own parent")
    normalized: dict[str, object] = {
        "generation_id": expected_id,
        "schema_version": _bounded_version("schema_version", value["schema_version"]),
        "collector_version": _bounded_version("collector_version", value["collector_version"]),
        "extractor_version": _bounded_version("extractor_version", value["extractor_version"]),
        "tokenizer_version": _bounded_version("tokenizer_version", value["tokenizer_version"]),
    }
    tokenizer_hash = value["tokenizer_config_sha256"]
    if not isinstance(tokenizer_hash, str) or _SHA256_RE.fullmatch(tokenizer_hash) is None:
        raise ValueError("tokenizer_config_sha256 must be lowercase SHA-256")
    normalized["tokenizer_config_sha256"] = tokenizer_hash

    embedding_id = _optional_version("embedding_model_id", value["embedding_model_id"])
    embedding_revision = _optional_version(
        "embedding_model_revision", value["embedding_model_revision"]
    )
    dimensions = value["vector_dimensions"]
    if dimensions is not None and (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or not 1 <= dimensions <= 65536
    ):
        raise ValueError("vector_dimensions must be null or a positive bounded integer")
    embedding_present = (
        embedding_id is not None,
        embedding_revision is not None,
        dimensions is not None,
    )
    if any(embedding_present) and not all(embedding_present):
        raise ValueError("embedding model ID, revision, and dimensions must be all set or null")
    normalized["embedding_model_id"] = embedding_id
    normalized["embedding_model_revision"] = embedding_revision
    normalized["vector_dimensions"] = dimensions

    graph_schema = _optional_version("graph_schema_version", value["graph_schema_version"])
    graph_extractor = _optional_version("graph_extractor_version", value["graph_extractor_version"])
    if (graph_schema is None) != (graph_extractor is None):
        raise ValueError("graph schema and extractor versions must be both set or null")
    normalized["graph_schema_version"] = graph_schema
    normalized["graph_extractor_version"] = graph_extractor
    if "parent_generation_id" in value:
        normalized["parent_generation_id"] = parent
    source_hash = value["source_manifest_sha256"]
    if not isinstance(source_hash, str) or _SHA256_RE.fullmatch(source_hash) is None:
        raise ValueError("source_manifest_sha256 must be lowercase SHA-256")
    normalized["source_manifest_sha256"] = source_hash
    content_digests = {"manifest.json": sha256_bytes(raw)}

    artifacts = value["artifacts"]
    if not isinstance(artifacts, list):
        raise TypeError("artifacts must be an array")
    if not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise ValueError("artifact count is outside the supported bounds")
    normalized_artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    total_size = 0
    for artifact in artifacts:
        _check_deadline(deadline, monotonic)
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            raise ValueError("each artifact must be a closed object")
        path_value = artifact["path"]
        if not isinstance(path_value, str):
            raise TypeError("artifact path must be a string")
        first = path_value.split("/", 1)[0]
        relative = restricted_relative_path(path_value, (first,))
        path_text = relative.as_posix()
        if path_text == "manifest.json" or path_text in seen:
            raise ValueError("artifact paths must be unique and exclude manifest.json")
        seen.add(path_text)
        size = artifact["size"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("artifact size is outside the supported bounds")
        total_size += size
        if total_size > MAX_GENERATION_BYTES:
            raise ValueError("generation artifact bytes exceed the supported bound")
        digest = artifact["sha256"]
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("artifact sha256 must be lowercase SHA-256")
        artifact_path = generation_path.joinpath(*relative.parts)
        actual_size, actual_digest = _hash_artifact(
            artifact_path,
            state_root,
            size,
            deadline=deadline,
            monotonic=monotonic,
        )
        if actual_size != size:
            raise ValueError(f"artifact has wrong size: {path_text}")
        if actual_digest != digest:
            raise ValueError(f"artifact has wrong hash: {path_text}")
        content_digests[path_text] = actual_digest
        normalized_artifacts.append({"path": path_text, "size": size, "sha256": digest})
    normalized_artifacts.sort(key=lambda item: str(item["path"]))
    if artifacts != normalized_artifacts:
        raise ValueError("artifacts must be normalized and sorted by path")
    normalized["artifacts"] = normalized_artifacts

    vector_state = value["vector_state"]
    if vector_state not in {"absent", "complete", "stale"}:
        raise ValueError("vector_state must be absent, complete, or stale")
    present_vectors = seen & _VECTOR_FILES
    if vector_state == "complete" and present_vectors != _VECTOR_FILES:
        raise ValueError("complete vectors require vectors.npy and vectors.json")
    if vector_state == "complete" and not all(embedding_present):
        raise ValueError("complete vectors require embedding metadata")
    if vector_state == "absent" and (present_vectors or any(embedding_present)):
        raise ValueError("absent vectors must not have vector artifacts or metadata")
    if present_vectors and present_vectors != _VECTOR_FILES:
        raise ValueError("partial vector artifacts are forbidden")
    if present_vectors and not all(embedding_present):
        raise ValueError("vector artifacts require embedding metadata")
    normalized["vector_state"] = vector_state

    if graph_schema == "evidence-graph/v1":
        from evidence_graph import validate_generation_artifact

        validate_generation_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            monotonic=monotonic,
        )

    final_seal, actual_files = _scan_generation(
        generation_path, deadline=deadline, monotonic=monotonic
    )
    if final_seal != initial_seal:
        raise PermissionError("generation changed during validation")
    if initial_files != actual_files or actual_files != seen | {"manifest.json"}:
        raise ValueError("manifest must bind every generation artifact")
    _check_deadline(deadline, monotonic)
    return normalized, _content_seal(final_seal, content_digests)


def validate_generation_manifest(
    generation_path: Path,
    *,
    state_root: Path,
) -> dict[str, object]:
    """Validate and return one closed, canonical generation manifest."""
    return _validate_generation(Path(generation_path), Path(state_root))[0]


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
        if (
            not isinstance(max_catalog_bytes, int)
            or isinstance(max_catalog_bytes, bool)
            or max_catalog_bytes < 1
        ):
            raise ValueError("max_catalog_bytes must be a positive integer")
        self.state_root = Path(state_root)
        self.max_catalog_bytes = max_catalog_bytes
        validate_state_root(self.state_root)
        default = self.state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        self.catalog_path = Path(catalog_path) if catalog_path is not None else default
        try:
            self.catalog_path.resolve(strict=False).relative_to(
                self.state_root.resolve(strict=True)
            )
        except ValueError as exc:
            raise ValueError("catalog_path must remain inside state_root") from exc
        self.generations_path = self.catalog_path.parent / "generations"
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.generations_path.mkdir(parents=True, exist_ok=True)
        fsync_directory(self.catalog_path.parent)
        fsync_directory(self.catalog_path.parent.parent)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        with closing(self._connect()) as database, database:
            self._ensure_schema(database)

    def _connect(self, *, deadline: float | None = None) -> sqlite3.Connection:
        self._check_deadline(deadline)
        if self.catalog_path.exists() or self.catalog_path.is_symlink():
            validate_runtime_file(
                self.catalog_path,
                self.state_root,
                max_bytes=self.max_catalog_bytes,
            )
        busy_ms = BUSY_MS
        if deadline is not None:
            busy_ms = self._remaining_busy_ms(deadline)
        return open_operational_db(self.catalog_path, busy_ms=busy_ms)

    def _check_deadline(self, deadline: float | None) -> None:
        if deadline is not None and (
            isinstance(deadline, bool) or not isinstance(deadline, (int, float))
        ):
            raise ValueError("deadline must be an absolute monotonic timestamp or None")
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("deadline must be an absolute monotonic timestamp or None")
        _check_deadline(deadline, self._monotonic)

    def _remaining_busy_ms(self, deadline: float) -> int:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("generation catalog deadline reached")
        return min(BUSY_MS, max(0, int(remaining * 1000)))

    @contextmanager
    def _write_transaction(self, deadline: float | None):
        self._check_deadline(deadline)
        with closing(self._connect(deadline=deadline)) as database:
            try:
                if deadline is not None:
                    database.execute(f"PRAGMA busy_timeout={self._remaining_busy_ms(deadline):d}")
                database.execute("BEGIN IMMEDIATE")
                self._check_deadline(deadline)
                yield database
                if deadline is not None:
                    database.execute(f"PRAGMA busy_timeout={self._remaining_busy_ms(deadline):d}")
                database.commit()
            except sqlite3.OperationalError as exc:
                database.rollback()
                if deadline is not None and "locked" in str(exc).casefold():
                    raise TimeoutError("generation catalog writer deadline reached") from exc
                raise
            except BaseException:
                database.rollback()
                raise

    def _readonly(self) -> sqlite3.Connection:
        return open_readonly_operational_db(
            self.catalog_path,
            self.state_root,
            max_bytes=self.max_catalog_bytes,
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
        self, generation_id: str, *, deadline: float | None = None
    ) -> tuple[dict[str, object], bytes, tuple[_EntrySeal, ...]]:
        identifier = _generation_id(generation_id)
        manifest, seal = _validate_generation(
            self.generations_path / identifier,
            state_root=self.state_root,
            deadline=deadline,
            monotonic=self._monotonic,
        )
        return manifest, canonical_json_bytes(manifest), seal

    def register(self, generation_id: str, *, deadline: float | None = None) -> dict[str, object]:
        """Register a complete generation; identical retries are idempotent."""
        self._check_deadline(deadline)
        manifest, encoded, seal = self._validate(generation_id, deadline=deadline)
        digest = sha256_bytes(encoded)
        timestamp = _utc_timestamp(self._clock)
        with self._write_transaction(deadline) as database:
            row = database.execute(
                "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if row is None:
                self._require_capacity(database, "generations", MAX_GENERATIONS, "generation")
                if not self._deadline_seal_unchanged(
                    self.generations_path / generation_id, seal, deadline
                ):
                    raise ValueError("generation changed after validation seal")
                database.execute(
                    "INSERT INTO generations "
                    "(generation_id, parent_generation_id, manifest_json, "
                    "manifest_sha256, registered_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        manifest.get("parent_generation_id"),
                        encoded,
                        digest,
                        timestamp,
                    ),
                )
                self._require_catalog_bytes(database)
            elif bytes(row["manifest_json"]) != encoded or row["manifest_sha256"] != digest:
                raise ValueError("generation registration is immutable")
            self._check_deadline(deadline)
        return manifest

    def _registered_generation(
        self, generation_id: str, *, deadline: float | None = None
    ) -> tuple[dict[str, object], tuple[_EntrySeal, ...]]:
        manifest, encoded, seal = self._validate(generation_id, deadline=deadline)
        self._check_deadline(deadline)
        with closing(self._readonly()) as database:
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
    ) -> bool:
        if deadline is None:
            return self._seal_unchanged(generation_path, expected)
        return self._seal_unchanged(generation_path, expected, deadline=deadline)

    def _seal_unchanged(
        self,
        generation_path: Path,
        expected: tuple[_EntrySeal, ...],
        *,
        deadline: float | None = None,
    ) -> bool:
        """Recheck bounded metadata and content immediately before catalog mutation."""
        current, files = _scan_generation(
            generation_path, deadline=deadline, monotonic=self._monotonic
        )
        expected_metadata = tuple(replace(entry, sha256=None) for entry in expected)
        if current != expected_metadata:
            return False
        expected_files = {entry.path: entry for entry in expected if entry.kind == "file"}
        if files != set(expected_files):
            return False
        for relative, entry in expected_files.items():
            self._check_deadline(deadline)
            _size, digest = _hash_artifact(
                generation_path.joinpath(*relative.split("/")),
                self.state_root,
                entry.size,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            if digest != entry.sha256:
                return False
        return True

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
        if (
            not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count < 0
            or not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size <= 0
        ):
            raise ValueError("catalog page metadata is invalid")
        # Reserve one page because commit may finalize a pending b-tree allocation.
        if page_count * page_size + page_size > self.max_catalog_bytes:
            raise ValueError("catalog byte ceiling would be exceeded")

    def activate(
        self,
        generation_id: str,
        *,
        expected_active: str | None,
        deadline: float | None = None,
    ) -> bool:
        """Atomically activate a validated generation if the pointer matches."""
        self._check_deadline(deadline)
        identifier = _generation_id(generation_id)
        if expected_active is not None:
            expected_active = _generation_id(expected_active)
        if deadline is None:
            _manifest, seal = self._registered_generation(identifier)
        else:
            _manifest, seal = self._registered_generation(identifier, deadline=deadline)
        timestamp = _utc_timestamp(self._clock)
        with self._write_transaction(deadline) as database:
            row = database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()
            if row is None or row["active_generation_id"] != expected_active:
                self._check_deadline(deadline)
                return False
            registered = database.execute(
                "SELECT 1 FROM generations WHERE generation_id = ?", (identifier,)
            ).fetchone()
            if registered is None:
                raise ValueError("generation registration disappeared")
            if identifier != expected_active:
                self._require_capacity(
                    database,
                    "activation_history",
                    MAX_ACTIVATION_HISTORY,
                    "history",
                )
            if not self._deadline_seal_unchanged(
                self.generations_path / identifier, seal, deadline
            ):
                raise ValueError("generation changed after validation seal")
            self._check_deadline(deadline)
            if identifier == expected_active:
                return True
            database.execute(
                "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
                (identifier,),
            )
            database.execute(
                "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
                (identifier, timestamp),
            )
            self._require_catalog_bytes(database)
            self._check_deadline(deadline)
        return True

    def _snapshot_catalog(
        self, *, deadline: float | None = None
    ) -> tuple[str | None, list[str], dict[str, str | None]]:
        self._check_deadline(deadline)
        with closing(self._readonly()) as database:
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
        self._check_deadline(deadline)
        return state["active_generation_id"], history, parents

    @staticmethod
    def _fallback_order(
        active: str | None, history: list[str], parents: dict[str, str | None]
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        prior = [identifier for identifier in [active, *history] if identifier is not None]
        for identifier in prior:
            if identifier not in seen:
                seen.add(identifier)
                ordered.append(identifier)
        for identifier in prior:
            parent = parents.get(identifier)
            while parent is not None and parent not in seen:
                seen.add(parent)
                ordered.append(parent)
                parent = parents.get(parent)
        return ordered

    def get_active(self, *, deadline: float | None = None) -> dict[str, object] | None:
        """Return a valid active generation, repairing a corrupt pointer safely."""
        for _attempt in range(3):
            self._check_deadline(deadline)
            active, history, parents = self._snapshot_catalog(deadline=deadline)
            selected: dict[str, object] | None = None
            selected_id: str | None = None
            selected_seal: tuple[_EntrySeal, ...] | None = None
            for identifier in self._fallback_order(active, history, parents):
                self._check_deadline(deadline)
                try:
                    if deadline is None:
                        selected, selected_seal = self._registered_generation(identifier)
                    else:
                        selected, selected_seal = self._registered_generation(
                            identifier, deadline=deadline
                        )
                except (FileNotFoundError, PermissionError, TypeError, ValueError):
                    continue
                selected_id = identifier
                break
            if selected_id == active:
                self._check_deadline(deadline)
                return selected
            timestamp = _utc_timestamp(self._clock)
            with self._write_transaction(deadline) as database:
                current = database.execute(
                    "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
                ).fetchone()
                if current is None:
                    raise ValueError("catalog active pointer is missing")
                if current["active_generation_id"] != active:
                    continue
                if selected_id is not None:
                    self._require_capacity(
                        database,
                        "activation_history",
                        MAX_ACTIVATION_HISTORY,
                        "history",
                    )
                if selected_id is not None and (
                    selected_seal is None
                    or not self._deadline_seal_unchanged(
                        self.generations_path / selected_id, selected_seal, deadline
                    )
                ):
                    raise ValueError("fallback generation changed after validation seal")
                database.execute(
                    "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
                    (selected_id,),
                )
                if selected_id is not None:
                    database.execute(
                        "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
                        (selected_id, timestamp),
                    )
                self._require_catalog_bytes(database)
                self._check_deadline(deadline)
            return selected
        raise sqlite3.OperationalError("active generation changed during fallback repair")

    def recover_orphans(self, *, deadline: float | None = None) -> list[str]:
        """Register complete immediate-child generations without activating them."""
        self._check_deadline(deadline)
        with closing(self._readonly()) as database:
            rows = self._bounded_rows(
                database,
                "SELECT generation_id FROM generations LIMIT ?",
                MAX_GENERATIONS,
                "generation",
            )
            registered = {row["generation_id"] for row in rows}
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
            except TimeoutError:
                if recovered:
                    return recovered
                raise
            if entry.name in registered or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                _generation_id(entry.name)
                if _is_link_or_reparse(Path(entry.path)):
                    continue
                if deadline is None:
                    self.register(entry.name)
                else:
                    self.register(entry.name, deadline=deadline)
            except TimeoutError:
                if recovered:
                    return recovered
                raise
            except (FileNotFoundError, PermissionError, TypeError, ValueError):
                continue
            recovered.append(entry.name)
        return recovered
