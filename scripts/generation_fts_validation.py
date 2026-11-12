"""Whether a generation's full-text index faithfully holds its corpus."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path

from corpus_snapshot import (
    MAX_CORPUS_FILE_BYTES,
    canonical_retrieval_chunks,
    validate_canonical_source_manifest,
)
from llm_wiki.formats.canonical import canonical_json_bytes
from reliable_memory import validate_runtime_file
from settings import raise_hint, setting_value

# v2 adds one indexed column, `keys`: the nightly fact keys of the turn, matched beside its
# text under one BM25 and never returned to a reader. A v1 artifact has no such column and
# stays readable. See `docs/research/2026-09-17-one-table-one-scale-for-the-keys.md`.
GENERATION_SEARCH_SCHEMA_VERSION = "corpus-search/v2"
LEGACY_SEARCH_SCHEMA_VERSION = "corpus-search/v1"
GENERATION_KEYS_COLUMN = "keys"
GENERATION_TOKENIZER = "porter unicode61"
GENERATION_TOKENIZER_VERSION = "sqlite-fts5/porter-unicode61/v1"
GENERATION_TOKENIZER_CONFIG_SHA256 = hashlib.sha256(
    GENERATION_TOKENIZER.encode("utf-8")
).hexdigest()
GENERATION_FTS_ARTIFACT = "search.sqlite3"


GENERATION_FTS_COLUMNS = (
    "chunk_id",
    "chunk_order",
    "source_id",
    "source_path",
    "source_sha256",
    "parent_page",
    "heading_ancestry",
    "byte_start",
    "byte_end",
    "line_start",
    "line_end",
    "span_sha256",
    "type",
    "project",
    "authority",
    "confidence",
    "status",
    "valid_from",
    "valid_to",
    "language",
    "title",
    "content",
)
GENERATION_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "collector_version",
        "extractor_version",
        "tokenizer_version",
        "tokenizer_config_sha256",
        "source_manifest_sha256",
        "chunk_count",
    }
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def generation_chunk_row(chunk: object, order: int) -> tuple[object, ...]:
    """The exact stored row for one chunk.

    The builder writes these and the validator rebuilds them from the
    authoritative sources to compare, so this shape is stated once: two copies
    of it would drift.
    """
    title = (
        chunk.heading_ancestry[-1]
        if chunk.heading_ancestry
        else Path(chunk.source_path).stem
    )
    return (
        chunk.id,
        order,
        chunk.source_id,
        chunk.source_path,
        chunk.source_sha256,
        chunk.parent_page,
        json.dumps(chunk.heading_ancestry, ensure_ascii=False, separators=(",", ":")),
        chunk.byte_start,
        chunk.byte_end,
        chunk.line_start,
        chunk.line_end,
        chunk.span_sha256,
        chunk.type,
        chunk.project,
        chunk.authority,
        chunk.confidence,
        chunk.status,
        chunk.valid_from,
        chunk.valid_to,
        chunk.language,
        title,
        chunk.text,
    )


def stop_reached(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    if cancelled is not None and cancelled():
        return True
    return deadline is not None and time.monotonic() >= deadline


def no_stop_requested(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    return deadline is None and cancelled is None


def check_generation_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("generation retrieval cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("generation retrieval deadline exceeded")


@contextmanager
def _sqlite_stop_guard(
    connection: sqlite3.Connection,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    *,
    check: Callable[[float | None, Callable[[], bool] | None], None],
    message: str,
) -> Iterator[None]:
    """Turn an interrupted SQLite statement into the stop that caused it."""
    check(deadline, cancelled)
    if no_stop_requested(deadline, cancelled):
        yield
        return
    stopped = False

    def progress() -> int:
        nonlocal stopped
        stopped = stop_reached(deadline, cancelled)
        return int(stopped)

    connection.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.DatabaseError as exc:
        if stopped:
            raise TimeoutError(message) from exc
        check(deadline, cancelled)
        raise
    finally:
        connection.set_progress_handler(None, 0)
    check(deadline, cancelled)


@contextmanager
def generation_sqlite_guard(
    connection: sqlite3.Connection,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[None]:
    with _sqlite_stop_guard(
        connection,
        deadline,
        cancelled,
        check=check_generation_stop,
        message="generation SQLite work cancelled or deadline exceeded",
    ):
        yield


def artifact_descriptors(manifest: Mapping[str, object]) -> dict[str, dict] | None:
    """One descriptor per artifact path, or None when the manifest is malformed."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    descriptors = {
        item.get("path"): item for item in artifacts if _is_path_descriptor(item)
    }
    if len(descriptors) != len(artifacts):
        return None
    return descriptors


def _is_path_descriptor(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("path"), str)


def valid_generation_fts(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    check_rows: bool = True,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    with generation_sqlite_guard(connection, deadline, cancelled):
        return _valid_generation_fts_contents(
            connection,
            manifest,
            check_rows=check_rows,
            deadline=deadline,
            cancelled=cancelled,
            authoritative_sources=authoritative_sources,
        )


def _read_bounded_chunks(
    descriptor: int,
    *,
    max_bytes: int,
    label: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[bytes]:
    chunks: list[bytes] = []
    total = 0
    while True:
        check_generation_stop(deadline, cancelled)
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return chunks
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"{label} exceeds its byte ceiling")
        chunks.append(chunk)


def _read_identity_stable_bytes(
    file_path: Path,
    expected: os.stat_result,
    *,
    max_bytes: int,
    label: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    """Read a file whose identity is proved unchanged before, during and after."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(file_path, flags)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(expected, opened):
            raise PermissionError(f"{label} identity changed before read")
        chunks = _read_bounded_chunks(
            descriptor,
            max_bytes=max_bytes,
            label=label,
            deadline=deadline,
            cancelled=cancelled,
        )
        after = os.fstat(descriptor)
        _require_same_file(opened, after, label)
    finally:
        os.close(descriptor)
    current = file_path.stat(follow_symlinks=False)
    if not os.path.samestat(after, current):
        raise PermissionError(f"{label} identity changed after read")
    return b"".join(chunks)


def _require_same_file(before: os.stat_result, after: os.stat_result, label: str) -> None:
    same_identity = os.path.samestat(before, after)
    same_content = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    if not same_identity or not same_content:
        raise PermissionError(f"{label} changed during read")


def validated_source_manifest(
    generation_path: Path,
    manifest: Mapping[str, object],
    *,
    state_root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict:
    source_manifest_path = generation_path / "source-manifest.json"
    expected = validate_runtime_file(
        source_manifest_path, state_root, max_bytes=setting_value("corpus.max_total_bytes")
    )
    raw = _read_identity_stable_bytes(
        source_manifest_path,
        expected,
        max_bytes=setting_value("corpus.max_total_bytes"),
        label="generation source manifest",
        deadline=deadline,
        cancelled=cancelled,
    )
    check_generation_stop(deadline, cancelled)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("generation source manifest is invalid") from exc
    source_manifest = validate_canonical_source_manifest(value)
    if canonical_json_bytes(source_manifest) != raw:
        raise ValueError("generation source manifest hash is invalid")
    if hashlib.sha256(raw).hexdigest() != manifest.get("source_manifest_sha256"):
        raise ValueError("generation source manifest hash is invalid")
    return source_manifest


def _all_ints(values: tuple[object, ...]) -> bool:
    return all(isinstance(value, int) for value in values)


def _valid_source_shape(
    relative_path: object, digest: object, size: object, content_size: object
) -> bool:
    return _all_strings((relative_path, digest)) and _all_ints((size, content_size))


def _valid_source_row(row: tuple[object, ...], seen: set[str]) -> bool:
    source_id, relative_path, digest, size, content_size = row
    if not isinstance(source_id, str) or source_id in seen:
        return False
    if not _valid_source_shape(relative_path, digest, size, content_size):
        return False
    return content_size <= MAX_CORPUS_FILE_BYTES and size == content_size


def _require_admissible_source_row(
    row: tuple, metadata: list, seen: set[str], max_files: int
) -> None:
    """One more row is allowed only under the ceiling, and only if it is valid."""
    if len(metadata) >= max_files:
        raise ValueError(f"generation source row ceiling exceeded; {raise_hint('corpus.max_files')}")
    if not _valid_source_row(row, seen):
        raise ValueError("generation evidence source rows are invalid")


def _source_metadata_rows(
    database: sqlite3.Connection,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[tuple[str, str, str, int]]:
    """Bounded source identity rows, refusing anything past a ceiling."""
    metadata: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    total_bytes = 0
    max_files = setting_value("corpus.max_files")
    max_total_bytes = setting_value("corpus.max_total_bytes")
    rows = database.execute(
        "SELECT source_id, relative_path, sha256, size, length(content) FROM source "
        "ORDER BY relative_path, source_id LIMIT ?",
        (max_files + 1,),
    )
    for row in rows:
        check_generation_stop(deadline, cancelled)
        _require_admissible_source_row(row, metadata, seen, max_files)
        source_id, relative_path, digest, size, content_size = row
        total_bytes += content_size
        if total_bytes > max_total_bytes:
            raise ValueError(
                f"generation evidence source bytes exceed their ceiling; {raise_hint('corpus.max_total_bytes')}"
            )
        seen.add(source_id)
        metadata.append((source_id, relative_path, digest, size))
    return metadata


def _verified_source_contents(
    database: sqlite3.Connection,
    metadata: list[tuple[str, str, str, int]],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, object]]:
    sources: dict[str, dict[str, object]] = {}
    for source_id, relative_path, digest, size in metadata:
        check_generation_stop(deadline, cancelled)
        row = database.execute(
            "SELECT content FROM source WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise ValueError("generation evidence source content is missing")
        content = bytes(row[0])
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("generation evidence source content is invalid")
        sources[source_id] = {
            "relative_path": relative_path,
            "sha256": digest,
            "content": content,
        }
    return sources


def _require_membership_matches(
    sources: Mapping[str, Mapping[str, object]], source_manifest: Mapping[str, object]
) -> None:
    membership = [
        {
            "logical_id": source_id,
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for source_id, source in sorted(
            sources.items(), key=lambda item: (str(item[1]["relative_path"]), item[0])
        )
    ]
    if membership != source_manifest["sources"]:
        raise ValueError("generation evidence source membership does not match source manifest")


class _AuthoritativeSources(dict):
    """Verified captured bytes with their already sealed collection context."""

    def __init__(self, sources, *, code_roots):
        super().__init__(sources)
        self._code_roots = tuple(code_roots)

    @property
    def code_roots(self):
        return self._code_roots


def generation_authoritative_sources(
    generation_path: Path,
    manifest: dict[str, object],
    *,
    state_root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, object]]:
    """Every source the generation claims, read back and verified byte for byte."""
    check_generation_stop(deadline, cancelled)
    source_manifest = validated_source_manifest(
        generation_path,
        manifest,
        state_root=state_root,
        deadline=deadline,
        cancelled=cancelled,
    )
    evidence_path = generation_path / "evidence.sqlite3"
    validate_runtime_file(evidence_path, state_root, max_bytes=16 * 1024 * 1024 * 1024)
    uri = f"{evidence_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
        with generation_sqlite_guard(database, deadline, cancelled):
            metadata = _source_metadata_rows(
                database, deadline=deadline, cancelled=cancelled
            )
            sources = _verified_source_contents(
                database, metadata, deadline=deadline, cancelled=cancelled
            )
    _require_membership_matches(sources, source_manifest)
    return _AuthoritativeSources(
        sources, code_roots=source_manifest["policy"]["code_roots"]
    )


def _reproducible_by_this_extractor(manifest: Mapping[str, object]) -> bool:
    """Only this extractor's own chunks can be re-derived and compared."""
    from llm_wiki import extractor_versions

    return manifest.get("extractor_version") == extractor_versions.CORPUS_EXTRACTOR_VERSION


FTS_ROWS_VERDICT = "fts-chunks"


def fts_rows_digest(manifest: dict[str, object]) -> str | None:
    """The declared digest of the FTS artifact: the identity of its rows."""
    descriptors = artifact_descriptors(manifest) or {}
    digest = descriptors.get(GENERATION_FTS_ARTIFACT, {}).get("sha256")
    return digest if isinstance(digest, str) else None


def fts_rows_need_walking(
    state_root: Path, manifest: dict[str, object], deep: bool
) -> tuple[object, str | None, bool]:
    """The verdict cache, the rows' digest, and whether to walk them now."""
    from verified_artifacts import VerifiedArtifacts

    digest = fts_rows_digest(manifest)
    cache = VerifiedArtifacts(state_root)
    return cache, digest, deep or cache.verdict(FTS_ROWS_VERDICT, digest) is None


def remember_walked_rows(cache: object, digest: str | None, walked: bool) -> None:
    """A walk that accepted these bytes is not repeated in the next process."""
    if not walked:
        return
    cache.remember_verdict(FTS_ROWS_VERDICT, digest)
    cache.save()


def validate_generation_fts_artifact(
    generation_path: Path,
    manifest: dict[str, object],
    *,
    state_root: Path,
    deep: bool = True,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Fail closed unless a generation-local FTS artifact is semantically valid.

    `deep` re-derives every chunk from the stored sources and compares it row by
    row. That is a determinism check of our own chunker, so since 2026-09-12 it
    runs where the rows are created — publication and registration — and in
    `doctor`, not on every cold read: the read path is served by the artifact
    digest, the manifest's versions and the entry seal, which already pin every
    input. It cost 1.68 s of a 4.9 s cold answer on the installed vault. See
    `docs/research/2026-09-12-a-reader-checks-the-digest-a-writer-derives.md`.

    The rows' own invariants are a separate matter: a digest recomputed over
    damaged bytes cannot catch them, so a read still walks the rows — once per
    distinct artifact digest, after which the verdict is remembered. See
    `docs/research/2026-09-12-the-rows-are-checked-once-per-distinct-bytes.md`.

    The content check can only be asked of a generation this extractor built.
    An older extractor's rows are not reproducible here — the 2026-09-07
    generation checked by the v3 chunker failed at row 9,272 and the vault
    answered lexical-only for two days — so for those the artifact is
    validated structurally and served, and the nightly rebuilds it on the
    version mismatch. See
    `docs/research/2026-09-10-a-generation-outlives-its-extractor.md`.
    """
    check_generation_stop(deadline, cancelled)
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    cache, digest, check_rows = fts_rows_need_walking(state_root, manifest, deep)
    authoritative_sources = None
    if deep and _reproducible_by_this_extractor(manifest):
        authoritative_sources = generation_authoritative_sources(
            generation_path,
            manifest,
            state_root=state_root,
            deadline=deadline,
            cancelled=cancelled,
        )
    artifact = Path(generation_path) / GENERATION_FTS_ARTIFACT
    validate_runtime_file(artifact, state_root, max_bytes=16 * 1024 * 1024 * 1024)
    uri = f"{artifact.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
        if not valid_generation_fts(
            connection,
            manifest,
            check_rows=check_rows,
            deadline=deadline,
            cancelled=cancelled,
            authoritative_sources=authoritative_sources,
        ):
            raise ValueError("generation FTS search artifact is semantically invalid")
    remember_walked_rows(cache, digest, check_rows)
    check_generation_stop(deadline, cancelled)


class _UnusableAuthoritativeSource(Exception):
    """An authoritative source that cannot produce chunks at all."""


_FTS_METADATA_SCHEMA = (
    ("key", "TEXT", 0, 1),
    ("value", "TEXT", 1, 0),
)
_FTS_TABLE_PREFIX = "create virtual table chunks using fts5("
FTS_CHUNK_SELECT = (
    "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
    "parent_page, heading_ancestry, byte_start, byte_end, line_start, line_end, "
    "span_sha256, type, project, authority, confidence, status, valid_from, "
    "valid_to, language, title, content FROM chunks ORDER BY chunk_order"
)


def _indexed_columns(version: str | None) -> tuple[str, ...] | None:
    """The indexed columns an artifact of this version declares, or None for an unknown one."""
    if version == GENERATION_SEARCH_SCHEMA_VERSION:
        return ("title", "content", GENERATION_KEYS_COLUMN)
    if version == LEGACY_SEARCH_SCHEMA_VERSION:
        return ("title", "content")
    return None


def _declared_version(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM generation_metadata WHERE key = 'schema_version'"
    ).fetchone()
    if not row:
        return None
    return str(row[0])


def _expected_chunk_columns(indexed: tuple[str, ...]) -> tuple[str, ...]:
    return (*GENERATION_FTS_COLUMNS[:-2], *indexed)


def _valid_table_shapes(connection: sqlite3.Connection, indexed: tuple[str, ...]) -> bool:
    metadata_schema = tuple(
        (row[1], row[2].upper(), row[3], row[5])
        for row in connection.execute("PRAGMA table_info(generation_metadata)")
    )
    chunk_columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(chunks)"))
    return metadata_schema == _FTS_METADATA_SCHEMA and chunk_columns == _expected_chunk_columns(indexed)


def valid_fts_schema(connection: sqlite3.Connection) -> bool:
    """Integrity, table shapes, and the exact FTS5 declaration of the artifact's own version."""
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        return False
    indexed = _indexed_columns(_declared_version(connection))
    if indexed is None or not _valid_table_shapes(connection, indexed):
        return False
    return _valid_fts_declaration(connection, indexed)


def _expected_fts_arguments(indexed: tuple[str, ...]) -> list[str]:
    return [
        *(f"{column} unindexed" for column in GENERATION_FTS_COLUMNS[:-2]),
        *indexed,
        "tokenize = 'porter unicode61'",
    ]


def _chunks_table_sql(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='chunks'"
    ).fetchone()
    if not row or not isinstance(row[0], str):
        return None
    return " ".join(row[0].casefold().split())


def _declared_fts_arguments(connection: sqlite3.Connection) -> list[str] | None:
    normalized = _chunks_table_sql(connection)
    if normalized is None:
        return None
    if not normalized.startswith(_FTS_TABLE_PREFIX) or not normalized.endswith(")"):
        return None
    return [
        argument.strip()
        for argument in normalized[len(_FTS_TABLE_PREFIX) : -1].split(",")
    ]


def _valid_fts_declaration(connection: sqlite3.Connection, indexed: tuple[str, ...]) -> bool:
    return _declared_fts_arguments(connection) == _expected_fts_arguments(indexed)


def _valid_metadata_rows(rows: list[tuple[object, object]]) -> bool:
    if len(rows) != len(GENERATION_METADATA_KEYS):
        return False
    if {row[0] for row in rows} != GENERATION_METADATA_KEYS:
        return False
    return all(isinstance(row[1], str) for row in rows)


def _generation_metadata(connection: sqlite3.Connection) -> dict[str, str] | None:
    """Exactly the expected keys, all strings, or None."""
    rows = list(
        connection.execute(
            "SELECT key, value FROM generation_metadata LIMIT ?",
            (len(GENERATION_METADATA_KEYS) + 1,),
        )
    )
    if not _valid_metadata_rows(rows):
        return None
    return dict(rows)


def _metadata_matches_manifest(
    metadata: Mapping[str, str], manifest: Mapping[str, object]
) -> bool:
    if _indexed_columns(metadata.get("schema_version")) is None:
        return False
    expected = {
        "collector_version": manifest.get("collector_version"),
        "extractor_version": manifest.get("extractor_version"),
        "tokenizer_version": GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": GENERATION_TOKENIZER_CONFIG_SHA256,
        "source_manifest_sha256": manifest.get("source_manifest_sha256"),
    }
    return all(metadata.get(key) == value for key, value in expected.items())


def expected_source_chunks(
    source_id: str,
    source: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    code_roots: tuple[str, ...] = (),
) -> Iterator[object]:
    content = source["content"]
    if not isinstance(content, bytes):
        raise _UnusableAuthoritativeSource
    yield from canonical_retrieval_chunks(
        source_id=source_id,
        source_path=str(source["relative_path"]),
        source_sha256=str(source["sha256"]),
        content=content,
        code_roots=code_roots,
        extractor_version=str(manifest.get("extractor_version")),
        deadline=deadline,
        cancelled=cancelled,
    )


def expected_chunk_rows(authoritative_sources, *, manifest, deadline, cancelled):
    """Validate source availability, then derive only the consumed source."""
    if any(
        not isinstance(source.get("content"), bytes) for source in authoritative_sources.values()
    ):
        return None
    ordered = sorted(
        authoritative_sources.items(), key=lambda item: (str(item[1]["relative_path"]), item[0])
    )
    return _iter_expected_chunk_rows(
        ordered, manifest, deadline, cancelled,
        code_roots=getattr(authoritative_sources, "code_roots", ()),
    )


def _iter_expected_chunk_rows(ordered, manifest, deadline, cancelled, *, code_roots=()):
    order = 0
    for source_id, source in ordered:
        check_generation_stop(deadline, cancelled)
        chunks = expected_source_chunks(
            source_id, source, manifest=manifest, deadline=deadline, cancelled=cancelled,
            code_roots=code_roots,
        )
        for chunk in chunks:
            yield generation_chunk_row(chunk, order)
            order += 1


def stored_chunk_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])


def _chunk_count_agrees(count: int, metadata: Mapping[str, str]) -> bool:
    return metadata.get("chunk_count") == str(count)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_filled_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _fresh_chunk_id(chunk_id: object, order: int, chunk_order: object) -> bool:
    return _is_sha256(chunk_id) and chunk_order == order


def _valid_source_reference(
    source_id: object, source_path: object, parent_page: object
) -> bool:
    if not _is_filled_str(source_path):
        return False
    return source_id == f"source:{source_path}" and parent_page == source_path


def _valid_chunk_identity(row: tuple[object, ...], order: int) -> bool:
    chunk_id, chunk_order, source_id, source_path, source_sha256, parent_page = row[:6]
    if not _fresh_chunk_id(chunk_id, order, chunk_order):
        return False
    if not _valid_source_reference(source_id, source_path, parent_page):
        return False
    return _is_sha256(source_sha256)


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _ordered_bounds(start: object, end: object, *, floor: int) -> bool:
    """Both offsets are integers and the span runs forward from `floor`."""
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return floor <= start <= end


def _valid_chunk_span(row: tuple[object, ...], ancestry: object) -> bool:
    byte_start, byte_end, line_start, line_end, span_sha256 = row[7:12]
    return (
        _is_string_list(ancestry)
        and _ordered_bounds(byte_start, byte_end, floor=0)
        and _ordered_bounds(line_start, line_end, floor=1)
        and _is_sha256(span_sha256)
    )


def _all_optional_strings(values: tuple[object, ...]) -> bool:
    return all(value is None or isinstance(value, str) for value in values)


def _all_strings(values: tuple[object, ...]) -> bool:
    return all(isinstance(value, str) for value in values)


def _valid_chunk_text(row: tuple[object, ...]) -> bool:
    optional_text = (row[13], row[14], row[15], row[17], row[18], row[19])
    status_value, title, content = row[16], row[20], row[21]
    if not _is_filled_str(row[12]) or not _all_optional_strings(optional_text):
        return False
    if not _all_strings((status_value, title)):
        return False
    return isinstance(content, str) and bool(content.strip())


def _valid_stored_chunk(row: tuple[object, ...], order: int) -> bool:
    try:
        ancestry = json.loads(row[6])
    except (TypeError, json.JSONDecodeError):
        return False
    if not _valid_chunk_identity(row, order):
        return False
    if not _valid_chunk_span(row, ancestry):
        return False
    return _valid_chunk_text(row)


_NO_EXPECTED_ROW = object()


def chunk_row_mismatch(row, expected_chunks) -> bool:
    if expected_chunks is None:
        return False
    return row != next(expected_chunks, _NO_EXPECTED_ROW)


def expected_rows_end(expected_chunks) -> bool:
    if expected_chunks is None:
        return True
    return next(expected_chunks, _NO_EXPECTED_ROW) is _NO_EXPECTED_ROW


def _expected_count_agrees(expected_chunks, count, deadline, cancelled):
    if expected_chunks is None:
        return True
    actual = 0
    for _row in expected_chunks:
        check_generation_stop(deadline, cancelled)
        actual += 1
    return actual == count


def stored_chunks_match(
    connection: sqlite3.Connection,
    expected_chunks: Iterator[tuple[object, ...]] | None,
    *,
    count: int,
    check_rows: bool = True,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Every row, unless these exact bytes were already walked once.

    The row invariants — sha256 ids, contiguous order, a heading ancestry that is
    a JSON list, non-blank content, unique ids — are what a reader needs to hold
    for rows it is about to serve, and a manifest digest recomputed over damaged
    bytes cannot catch them. Walking 3 405 rows costs 0.38 s, so the walk is paid
    once per distinct artifact digest and its verdict is remembered; the caller
    decides with `check_rows`. Comparing each row against a freshly chunked
    source stays with the re-derivation that asks for it. See
    `docs/research/2026-09-12-the-rows-are-checked-once-per-distinct-bytes.md`.
    """
    if not check_rows:
        return _expected_count_agrees(expected_chunks, count, deadline, cancelled)
    return rows_hold_invariants(
        connection, expected_chunks, count=count, deadline=deadline, cancelled=cancelled
    )


def rows_hold_invariants(
    connection: sqlite3.Connection,
    expected_chunks: Iterator[tuple[object, ...]] | None,
    *,
    count: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    check_generation_stop(deadline, cancelled)
    unique = _unique_stored_chunk_ids(connection)
    check_generation_stop(deadline, cancelled)
    if not unique:
        return False
    return _stored_rows_agree(connection, expected_chunks, count, deadline, cancelled)


def _unique_stored_chunk_ids(connection: sqlite3.Connection) -> bool:
    """SQLite checks uniqueness with its file-backed transient index.

    These private artifact connections hold no TEMP tables. FILE avoids a
    Python ID set and permits pager spill on the qualified SQLite runtime.
    """
    connection.execute("PRAGMA temp_store=FILE")
    row = connection.execute(
        "SELECT COUNT(*) = COUNT(DISTINCT chunk_id) FROM chunks"
    ).fetchone()
    return bool(row[0])


def _stored_rows_agree(connection, expected_chunks, count, deadline, cancelled) -> bool:
    observed = 0
    for order, row in enumerate(connection.execute(FTS_CHUNK_SELECT)):
        check_generation_stop(deadline, cancelled)
        if not _valid_stored_chunk(row, order):
            return False
        if chunk_row_mismatch(row, expected_chunks):
            return False
        observed += 1
    return observed == count and expected_rows_end(expected_chunks)


_UNUSABLE_CHUNKS = object()


def _valid_fts_metadata(
    connection: sqlite3.Connection, manifest: dict[str, object]
) -> dict | None:
    """The metadata row, or None when the artifact does not describe itself."""
    if not valid_fts_schema(connection):
        return None
    metadata = _generation_metadata(connection)
    if metadata is None or not _metadata_matches_manifest(metadata, manifest):
        return None
    return metadata


def _expected_chunks(
    authoritative_sources: dict[str, dict[str, object]] | None,
    manifest: dict[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
):
    """Rows the sources must produce, None when unchecked, sentinel when unusable."""
    if authoritative_sources is None:
        return None
    rows = expected_chunk_rows(
        authoritative_sources,
        manifest=manifest,
        deadline=deadline,
        cancelled=cancelled,
    )
    if rows is None:
        return _UNUSABLE_CHUNKS
    return rows


def _valid_generation_fts_contents(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    check_rows: bool = True,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    """Every structural claim the FTS artifact makes about itself must hold."""
    metadata = _valid_fts_metadata(connection, manifest)
    if metadata is None:
        return False
    expected_chunks = _expected_chunks(
        authoritative_sources, manifest, deadline, cancelled
    )
    if expected_chunks is _UNUSABLE_CHUNKS:
        return False
    return _fts_counts_and_chunks_match(
        connection,
        metadata,
        expected_chunks,
        check_rows=check_rows,
        deadline=deadline,
        cancelled=cancelled,
    )


def _fts_counts_and_chunks_match(
    connection: sqlite3.Connection,
    metadata: object,
    expected_chunks: object,
    *,
    check_rows: bool = True,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """The stored chunk count, and then the stored chunks themselves."""
    count = stored_chunk_count(connection)
    if not _chunk_count_agrees(count, metadata):
        return False
    return stored_chunks_match(
        connection,
        expected_chunks,
        count=count,
        check_rows=check_rows,
        deadline=deadline,
        cancelled=cancelled,
    )
