"""Built-in hybrid search over the vault — zero external dependencies.

Uses Python's built-in sqlite3 + FTS5 for BM25 full-text search.
Optionally uses sentence-transformers for semantic (vector) search
when the library is installed. Results are fused via Reciprocal
Rank Fusion (RRF) for hybrid ranking.

For solo-developer vaults (<500 pages):
- BM25 only: <10ms, zero deps, good for keyword-precise queries
- BM25 + Vector: <50ms, needs `pip install sentence-transformers`,
  finds semantically related pages ("database performance" → "N+1 query fix")

Usage:
    uv run python scripts/search_memory.py "auth decision"
    uv run python scripts/search_memory.py "database performance" --semantic
    uv run python scripts/search_memory.py "hook timing gotcha" --limit 5
    uv run python scripts/search_memory.py "JWT" --scope wiki --project your-project
    uv run python scripts/search_memory.py --rebuild  # force index rebuild
    uv run python scripts/search_memory.py --status   # show index stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from corpus_snapshot import (  # noqa: E402
    MAX_CORPUS_FILE_BYTES,
    MAX_CORPUS_FILES,
    MAX_CORPUS_TOTAL_BYTES,
    CorpusSnapshot,
    canonical_retrieval_chunks,
    validate_canonical_source_manifest,
    validate_live_snapshot,
)
from generation_catalog import GenerationCatalog  # noqa: E402
from memory_state import ROOT, STATE_ROOT, _is_pid_alive, atomic_write  # noqa: E402
from provenance import authority_weight  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    validate_runtime_file,
)

INDEX_DIR = STATE_ROOT / "cache"
INDEX_FILE = INDEX_DIR / "index.sqlite"
INDEX_MANIFEST = INDEX_DIR / ".paths-manifest"
VECTOR_NPY = INDEX_DIR / "vectors.npy"  # Binary numpy cache (memory-mapped, fast load)
VECTOR_META = INDEX_DIR / "vectors_meta.json"  # Metadata without vectors (small JSON)

_INDEX_SWAP_WAIT_SECONDS = 10.0
_INDEX_SWAP_STALE_SECONDS = 30.0
_INDEX_REPLACE_WAIT_SECONDS = 1.0
MAX_SEARCHABLE_PAGES = 10_000
MAX_SEARCH_ENTRIES = 20_000
MAX_SEARCH_DIRECTORIES = 2_000
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_LIMIT = 1_000
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PATH_MANIFEST_BYTES = 4 * 1024 * 1024
SEARCH_INDEX_COLUMNS = (
    "path", "title", "summary", "body", "project", "timestamp", "slug",
)
GENERATION_SEARCH_SCHEMA_VERSION = "corpus-search/v1"
GENERATION_TOKENIZER = "porter unicode61"
GENERATION_TOKENIZER_VERSION = "sqlite-fts5/porter-unicode61/v1"
GENERATION_TOKENIZER_CONFIG_SHA256 = hashlib.sha256(
    GENERATION_TOKENIZER.encode("utf-8")
).hexdigest()
GENERATION_FTS_ARTIFACT = "search.sqlite3"
GENERATION_VECTOR_ARTIFACTS = ("vectors.json", "vectors.npy")
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
MAX_GENERATION_FTS_CHUNKS = 100_000
GENERATION_FTS_PROGRESS_OPCODES = 1_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class LegacySearchUnavailable(RuntimeError):
    """The derived SQLite index failed and direct Markdown found no match."""


KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
# Legacy alias retained for tests and external callers. Post-three-zone
# consolidation both names resolve to the same single knowledge/notes tree.
WIKI_DIR = KNOWLEDGE_DIR

# Files to skip (editorial / operational, not knowledge)
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}
SKIP_DIRS = {"projects", "gaps", "raw-sources"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)

# Embedding model — bge-small-en-v1.5 (MIT, MTEB 62.17, +25% over MiniLM).
# Same 384d as all-MiniLM-L6-v2 — no dimension change needed.
# Query instruction prefix improves retrieval accuracy (per BGE model card).
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
EMBEDDING_DIM = 384
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages:"


def _have_sentence_transformers() -> bool:
    """Check if sentence-transformers is importable."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_embedder():
    """Lazily load the embedding model. Returns None if unavailable.

    The model is cached at module level — loading ~90MB model once,
    not per-query. This is critical for benchmark latency.
    """
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache
    try:
        from sentence_transformers import SentenceTransformer
        _embedder_cache = SentenceTransformer(
            EMBEDDING_MODEL, revision=EMBEDDING_MODEL_REVISION
        )
        return _embedder_cache
    except Exception:
        return None


_embedder_cache = None


def _validate_search_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SEARCH_LIMIT
    ):
        raise ValueError(f"limit must be an integer from 1 to {MAX_SEARCH_LIMIT}")
    return value


def _cli_search_limit(value: str) -> int:
    try:
        return _validate_search_limit(int(value))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _artifact_descriptor(
    path: Path,
    relative: str,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as artifact:
        while chunk := artifact.read(64 * 1024):
            _check_generation_stop(deadline, cancelled)
            size += len(chunk)
            digest.update(chunk)
    return {
        "path": relative,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _generation_directory(directory: Path) -> Path:
    selected = Path(directory)
    info = selected.lstat()
    if selected.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("generation output must be an existing regular directory")
    return selected


def _publish_new_file(temporary: Path, destination: Path) -> None:
    """Atomically expose a completed file without replacing an existing artifact."""
    try:
        os.link(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


_GENERATION_FTS_DDL = """
            CREATE TABLE generation_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE VIRTUAL TABLE chunks USING fts5(
                chunk_id UNINDEXED,
                chunk_order UNINDEXED,
                source_id UNINDEXED,
                source_path UNINDEXED,
                source_sha256 UNINDEXED,
                parent_page UNINDEXED,
                heading_ancestry UNINDEXED,
                byte_start UNINDEXED,
                byte_end UNINDEXED,
                line_start UNINDEXED,
                line_end UNINDEXED,
                span_sha256 UNINDEXED,
                type UNINDEXED,
                project UNINDEXED,
                authority UNINDEXED,
                confidence UNINDEXED,
                status UNINDEXED,
                valid_from UNINDEXED,
                valid_to UNINDEXED,
                language UNINDEXED,
                title,
                content,
                tokenize = 'porter unicode61'
            );
            """


def _generation_fts_metadata(snapshot: CorpusSnapshot) -> dict[str, str]:
    return {
        "schema_version": GENERATION_SEARCH_SCHEMA_VERSION,
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": GENERATION_TOKENIZER_CONFIG_SHA256,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "chunk_count": str(len(snapshot.chunks)),
    }


def _generation_chunk_row(chunk: object, order: int) -> tuple[object, ...]:
    """The exact stored row for one chunk.

    The builder writes these and the validator rebuilds them from the
    authoritative sources to compare, so this shape is stated once.
    """
    """The exact stored row for one chunk.

    The builder writes these and the validator rebuilds them from the
    authoritative sources to compare; two copies of this shape would drift.
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


def _write_generation_fts(
    database: sqlite3.Connection,
    snapshot: CorpusSnapshot,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Schema, metadata and every chunk, verified before the caller publishes."""
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    database.executescript(_GENERATION_FTS_DDL)
    database.executemany(
        "INSERT INTO generation_metadata(key, value) VALUES (?, ?)",
        sorted(_generation_fts_metadata(snapshot).items()),
    )

    def rows():
        for order, chunk in enumerate(snapshot.chunks):
            _check_generation_stop(deadline, cancelled)
            yield _generation_chunk_row(chunk, order)

    database.executemany(
        "INSERT INTO chunks VALUES (" + ",".join("?" for _ in range(22)) + ")",
        rows(),
    )
    database.commit()
    if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ValueError("generation FTS integrity check failed")


def build_generation_fts(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Build one immutable generation-local FTS5 artifact from captured chunks."""
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    _check_generation_stop(deadline, cancelled)
    if len(snapshot.chunks) > MAX_GENERATION_FTS_CHUNKS:
        raise ValueError("generation FTS chunk row ceiling exceeded")
    directory = _generation_directory(generation_directory)
    destination = directory / GENERATION_FTS_ARTIFACT
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    temporary = directory / f".{GENERATION_FTS_ARTIFACT}.{uuid.uuid4().hex}.tmp"
    database = None
    stopped = False
    complete = False

    def progress() -> int:
        nonlocal stopped
        stopped = bool(cancelled and cancelled()) or bool(
            deadline is not None and time.monotonic() >= deadline
        )
        return int(stopped)

    try:
        database = sqlite3.connect(temporary)
        database.set_progress_handler(progress, GENERATION_FTS_PROGRESS_OPCODES)
        _write_generation_fts(
            database, snapshot, deadline=deadline, cancelled=cancelled
        )
        database.close()
        database = None
        fsync_file(temporary)
        _check_generation_stop(deadline, cancelled)
        _publish_new_file(temporary, destination)
        fsync_directory(directory)
        descriptor = _artifact_descriptor(
            destination,
            GENERATION_FTS_ARTIFACT,
            deadline=deadline,
            cancelled=cancelled,
        )
        complete = True
        return descriptor
    except sqlite3.DatabaseError as exc:
        if stopped:
            raise TimeoutError("generation FTS build cancelled or deadline reached") from exc
        raise
    finally:
        if database is not None:
            database.close()
        if not complete:
            _remove_quietly((destination,))
        _remove_quietly((temporary,))

def _call_generation_embedder(embedder: object, texts: list[str]):
    if callable(embedder):
        return embedder(texts)
    encode = getattr(embedder, "encode", None)
    if not callable(encode):
        raise TypeError("embedder must be callable or provide encode()")
    return encode(texts, show_progress_bar=False, convert_to_numpy=True)


def _require_vector_build_inputs(
    snapshot: object, model_id: str, model_revision: str, dimensions: int
) -> None:
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if not model_id or not model_revision:
        raise ValueError("model ID and revision must be non-empty")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("dimensions must be a positive integer")


def _require_absent_artifacts(destinations: list[Path]) -> None:
    for destination in destinations:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)


def _embedded_matrix(
    snapshot: CorpusSnapshot, embedder: object, dimensions: int
) -> object:
    """The embedder's output, refused unless it is finite and the right shape."""
    import numpy as np

    matrix = np.asarray(_call_generation_embedder(embedder, [c.text for c in snapshot.chunks]))
    if matrix.ndim != 2 or matrix.shape != (len(snapshot.chunks), dimensions):
        raise ValueError("embedder returned a matrix with incompatible shape")
    if matrix.dtype.kind not in "fiu" or not np.isfinite(matrix).all():
        raise ValueError("embedder returned a non-finite numeric matrix")
    return np.ascontiguousarray(matrix, dtype=np.float32)


def _vector_metadata(
    snapshot: CorpusSnapshot, *, model_id: str, model_revision: str, dimensions: int
) -> dict[str, object]:
    return {
        "schema_version": "corpus-vectors/v1",
        "corpus_sha256": snapshot.corpus_sha256,
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "model_id": model_id,
        "model_revision": model_revision,
        "dimensions": dimensions,
        "chunk_ids": [chunk.id for chunk in snapshot.chunks],
        "source_ids": [chunk.source_id for chunk in snapshot.chunks],
        "source_paths": [chunk.source_path for chunk in snapshot.chunks],
        "source_sha256": [chunk.source_sha256 for chunk in snapshot.chunks],
    }


def _remove_quietly(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _publish_vector_artifacts(
    directory: Path,
    destinations: list[Path],
    metadata: Mapping[str, object],
    matrix: object,
) -> None:
    """Write both artifacts to temporaries and publish them, or leave neither."""
    import numpy as np

    temporary_json = directory / f".vectors.json.{uuid.uuid4().hex}.tmp"
    temporary_npy = directory / f".vectors.npy.{uuid.uuid4().hex}.tmp"
    created: list[Path] = []
    try:
        temporary_json.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="",
        )
        with temporary_npy.open("wb") as output:
            np.save(output, matrix, allow_pickle=False)
        for temporary, destination in zip(
            (temporary_json, temporary_npy), destinations, strict=True
        ):
            _publish_new_file(temporary, destination)
            created.append(destination)
    except BaseException:
        _remove_quietly(created)
        raise
    finally:
        _remove_quietly((temporary_json, temporary_npy))


def build_generation_numpy_vectors(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    dimensions: int,
) -> list[dict[str, object]]:
    """Build an exact NumPy matrix and closed metadata from one chunk sequence."""
    _require_vector_build_inputs(snapshot, model_id, model_revision, dimensions)
    directory = _generation_directory(generation_directory)
    destinations = [directory / name for name in GENERATION_VECTOR_ARTIFACTS]
    _require_absent_artifacts(destinations)
    _publish_vector_artifacts(
        directory,
        destinations,
        _vector_metadata(
            snapshot,
            model_id=model_id,
            model_revision=model_revision,
            dimensions=dimensions,
        ),
        _embedded_matrix(snapshot, embedder, dimensions),
    )
    return [
        _artifact_descriptor(directory / name, name)
        for name in GENERATION_VECTOR_ARTIFACTS
    ]


def publish_generation(
    snapshot: CorpusSnapshot,
    vault: Path,
    catalog: GenerationCatalog,
    generation_id: str,
    *,
    expected_active: str | None,
    coordinator: object | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Fence live bytes, register a complete generation, then CAS-activate it."""
    return _publish_generation(
        snapshot,
        vault,
        catalog,
        generation_id,
        expected_active=expected_active,
        coordinator=coordinator,
        deadline=deadline,
        cancelled=cancelled,
    )


def _publish_validated_generation(
    snapshot: CorpusSnapshot,
    vault: Path,
    catalog: GenerationCatalog,
    generation_id: str,
    candidate: object,
    *,
    expected_repository_scope: object,
    expected_active: str | None,
    coordinator: object | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Publish a catalog-minted candidate while preserving the live-source fence."""
    return _publish_generation(
        snapshot,
        vault,
        catalog,
        generation_id,
        candidate=candidate,
        expected_repository_scope=expected_repository_scope,
        expected_active=expected_active,
        coordinator=coordinator,
        deadline=deadline,
        cancelled=cancelled,
    )


def _require_finite_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp")


def _publication_gate(
    coordinator: object | None, wait_seconds: float | None
) -> AbstractContextManager[object]:
    """The Markdown writer gate, or nothing when no coordinator was supplied."""
    if coordinator is None:
        return nullcontext()
    if wait_seconds is None:
        return coordinator.writer_gate()
    return coordinator.writer_gate(wait_seconds=wait_seconds)


def _require_matching_repository(
    vault: Path,
    expected_repository_scope: object | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if expected_repository_scope is None:
        return
    from repository_scope import RepositoryScope, resolve_repository_scope

    if not isinstance(expected_repository_scope, RepositoryScope):
        raise TypeError("expected_repository_scope must be a RepositoryScope")
    live_scope = resolve_repository_scope(vault, deadline=deadline, cancelled=cancelled)
    if live_scope != expected_repository_scope:
        raise ValueError("publication root does not match generation repository scope")


def _discard_unactivated(
    catalog: GenerationCatalog, generation_id: str, catalog_options: Mapping[str, object]
) -> None:
    discard = getattr(catalog, "discard_unactivated", None)
    if callable(discard):
        discard(generation_id, **catalog_options)


def _register_generation(
    catalog: GenerationCatalog,
    generation_id: str,
    candidate: object | None,
    catalog_options: Mapping[str, object],
) -> None:
    if candidate is None:
        catalog.register(generation_id, **catalog_options)
        return
    catalog._register_validated(candidate, **catalog_options)  # noqa: SLF001


def _activate_generation(
    catalog: GenerationCatalog,
    generation_id: str,
    candidate: object | None,
    *,
    expected_active: str | None,
    catalog_options: Mapping[str, object],
) -> bool:
    if candidate is None:
        return bool(
            catalog.activate(
                generation_id, expected_active=expected_active, **catalog_options
            )
        )
    return bool(
        catalog._activate_validated(  # noqa: SLF001
            candidate, expected_active=expected_active, **catalog_options
        )
    )


def _stop_options(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> dict[str, object]:
    options: dict[str, object] = {}
    if deadline is not None:
        options["deadline"] = float(deadline)
    if cancelled is not None:
        options["cancelled"] = cancelled
    return options


def _publish_under_gate(
    snapshot: CorpusSnapshot,
    vault: Path,
    catalog: GenerationCatalog,
    generation_id: str,
    *,
    candidate: object | None,
    expected_repository_scope: object | None,
    expected_active: str | None,
    remaining: Callable[[], float | None],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Validate, register, activate — and discard anything left unactivated."""
    catalog_options = _stop_options(deadline, cancelled)
    registered = False
    try:
        _check_generation_stop(deadline, cancelled)
        _require_matching_repository(
            vault, expected_repository_scope, deadline=deadline, cancelled=cancelled
        )
        validation_options: dict[str, object] = {"coordinator": None}
        if cancelled is not None:
            validation_options["cancelled"] = cancelled
        if deadline is not None:
            validation_options["deadline_seconds"] = remaining()
        validate_live_snapshot(snapshot, vault, **validation_options)
        remaining()
        _register_generation(catalog, generation_id, candidate, catalog_options)
        registered = True
        remaining()
        activated = _activate_generation(
            catalog,
            generation_id,
            candidate,
            expected_active=expected_active,
            catalog_options=catalog_options,
        )
        if not activated:
            _discard_unactivated(catalog, generation_id, catalog_options)
        return activated
    except BaseException:
        if registered:
            _discard_unactivated(catalog, generation_id, catalog_options)
        raise


def _publish_generation(
    snapshot: CorpusSnapshot,
    vault: Path,
    catalog: GenerationCatalog,
    generation_id: str,
    *,
    candidate: object | None = None,
    expected_repository_scope: object | None = None,
    expected_active: str | None,
    coordinator: object | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    _require_finite_deadline(deadline)

    def remaining() -> float | None:
        _check_generation_stop(deadline, cancelled)
        if deadline is None:
            return None
        value = float(deadline) - time.monotonic()
        if value <= 0:
            raise TimeoutError("generation publication deadline reached")
        return value

    with _publication_gate(coordinator, remaining()):
        return _publish_under_gate(
            snapshot,
            vault,
            catalog,
            generation_id,
            candidate=candidate,
            expected_repository_scope=expected_repository_scope,
            expected_active=expected_active,
            remaining=remaining,
            deadline=deadline,
            cancelled=cancelled,
        )


def _check_legacy_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("legacy retrieval cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("legacy retrieval deadline reached")


@contextmanager
def _legacy_sqlite_guard(
    connection: sqlite3.Connection,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[None]:
    _check_legacy_stop(deadline, cancelled)
    if deadline is None and cancelled is None:
        yield
        return
    stopped = False

    def progress() -> int:
        nonlocal stopped
        stopped = bool(cancelled and cancelled()) or bool(
            deadline is not None and time.monotonic() >= deadline
        )
        return int(stopped)

    connection.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.DatabaseError as exc:
        if stopped:
            raise TimeoutError(
                "legacy SQLite work cancelled or deadline exceeded"
            ) from exc
        _check_legacy_stop(deadline, cancelled)
        raise
    finally:
        connection.set_progress_handler(None, 0)
    _check_legacy_stop(deadline, cancelled)


def _embed_texts(
    texts: list[str],
    is_query: bool = False,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[float]] | None:
    """Embed a list of texts. Returns None if model unavailable.

    For bge-small-en-v1.5, queries are prefixed with a retrieval instruction
    for better accuracy. Documents are embedded without prefix.
    """
    _check_legacy_stop(deadline, cancelled)
    embedder = _get_embedder()
    if not embedder:
        return None
    try:
        if is_query and QUERY_INSTRUCTION:
            texts = [f"{QUERY_INSTRUCTION} {t}" for t in texts]
        vectors = embedder.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        _check_legacy_stop(deadline, cancelled)
        return vectors.tolist()
    except TimeoutError:
        raise
    except Exception:
        return None


def _cosine_similarity(query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]:
    """Compute cosine similarity between query and all documents."""
    import numpy as np
    q = np.array(query_vec)
    docs = np.array(doc_vecs)
    # Normalize
    q_norm = q / (np.linalg.norm(q) + 1e-10)
    docs_norm = docs / (np.linalg.norm(docs, axis=1, keepdims=True) + 1e-10)
    return (docs_norm @ q_norm).tolist()


class _PageWalkLimits:
    """Bounded traversal budget for one page collection."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.entries = 0
        self.directories = 0

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("searchable page collection deadline reached")

    def count_directory(self) -> None:
        self.directories += 1
        if self.directories > MAX_SEARCH_DIRECTORIES:
            raise ValueError("searchable directory limit exceeded")

    def count_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_SEARCH_ENTRIES:
            raise ValueError("searchable entry limit exceeded")


def _is_safe_entry(path: Path, *, directory: bool) -> bool:
    """No symlinks, no reparse points, and the kind the caller expects."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if path.is_symlink():
        return False
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if reparse:
        return False
    if directory:
        return stat.S_ISDIR(info.st_mode)
    return stat.S_ISREG(info.st_mode)


def _require_safe_ancestry(root: Path, source_root: Path) -> None:
    """Every directory from the vault down to the root must be safe."""
    try:
        relative_root = root.relative_to(source_root)
    except ValueError as exc:
        raise OSError("searchable knowledge root escapes the vault") from exc
    component = source_root
    components = [source_root]
    for part in relative_root.parts:
        component /= part
        components.append(component)
    if not all(_is_safe_entry(item, directory=True) for item in components):
        raise OSError("unsafe knowledge directory")


def _scan_directory(
    current_path: Path, limits: _PageWalkLimits
) -> tuple[list[Path], list[str]] | None:
    """(safe subdirectories, markdown names), or None when it cannot be read."""
    directories: list[Path] = []
    filenames: list[str] = []
    try:
        with os.scandir(current_path) as entries:
            for entry in entries:
                limits.check_deadline()
                limits.count_entry()
                path = current_path / entry.name
                if entry.name not in SKIP_DIRS and _is_safe_entry(path, directory=True):
                    directories.append(path)
                elif entry.name.endswith(".md"):
                    filenames.append(entry.name)
    except OSError:
        return None
    return directories, filenames


def _is_retired_page(content: str) -> bool:
    """Superseded and archived pages are history, not search results."""
    frontmatter = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not frontmatter:
        return False
    status = re.search(r"^status:\s*(.+?)\s*$", frontmatter.group(1), re.MULTILINE)
    return bool(status) and status.group(1).strip() in ("superseded", "archived")


def _searchable_page(md: Path, name: str, seen: set[Path]) -> bool:
    if name in SKIP_NAMES or md in seen:
        return False
    if not _is_safe_entry(md, directory=False):
        return False
    try:
        raw = read_stable_bytes(md, MAX_PAGE_BYTES, label="search page")
    except (OSError, ValueError):
        return False
    return not _is_retired_page(raw.decode("utf-8", errors="ignore"))


def _collect_directory_pages(
    current_path: Path,
    filenames: list[str],
    *,
    pages: list[Path],
    seen: set[Path],
    limits: _PageWalkLimits,
) -> None:
    for name in sorted(filenames):
        limits.check_deadline()
        md = current_path / name
        if not _searchable_page(md, name, seen):
            continue
        seen.add(md)
        pages.append(md)
        if len(pages) > MAX_SEARCHABLE_PAGES:
            raise ValueError("searchable page limit exceeded")


def _walk_knowledge_root(
    root: Path,
    *,
    pages: list[Path],
    seen: set[Path],
    limits: _PageWalkLimits,
) -> None:
    pending = [(root, 0)]
    while pending:
        current_path, depth = pending.pop()
        limits.check_deadline()
        limits.count_directory()
        if depth > MAX_SEARCH_DEPTH:
            raise ValueError("searchable directory depth limit exceeded")
        scanned = _scan_directory(current_path, limits)
        if scanned is None:
            continue
        directories, filenames = scanned
        if depth >= MAX_SEARCH_DEPTH and directories:
            raise ValueError("searchable directory depth limit exceeded")
        limits.check_deadline()
        _collect_directory_pages(
            current_path, filenames, pages=pages, seen=seen, limits=limits
        )
        limits.check_deadline()
        pending.extend((directory, depth + 1) for directory in reversed(sorted(directories)))


def _collect_pages(
    scope: str = "all",
    *,
    knowledge_dir: Path | None = None,
    root: Path | None = None,
    deadline: float = float("inf"),
) -> list[Path]:
    """Collect bounded regular markdown pages without following links."""
    # Every scope resolves to the one knowledge/notes tree after the three-zone
    # consolidation; "wiki" and "memory" remain accepted aliases.
    if scope not in ("wiki", "memory", "knowledge", "all"):
        return []
    selected_knowledge = knowledge_dir or KNOWLEDGE_DIR
    source_root = root or selected_knowledge.parents[1]
    limits = _PageWalkLimits(deadline)
    limits.check_deadline()
    if not selected_knowledge.exists():
        return []
    _require_safe_ancestry(selected_knowledge, source_root)
    pages: list[Path] = []
    _walk_knowledge_root(
        selected_knowledge, pages=pages, seen=set(), limits=limits
    )
    return pages


def _extract_frontmatter_field(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


# Patterns for metadata extraction
PROJECT_FIELD_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TIMESTAMP_FIELD_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)
AUTHORITY_FIELD_RE = re.compile(
    r"^source_authority:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
)
VALID_TO_FIELD_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)



def _extract_title_and_summary(content: str, fallback_stem: str) -> tuple[str, str]:
    title = fallback_stem
    summary = ""
    # Strip frontmatter for cleaner search
    body = FRONTMATTER_RE.sub("", content, count=1)
    m = H1_RE.search(body)
    if m:
        title = m.group(1).strip()
    m = SUMMARY_RE.search(body)
    if m:
        summary = m.group(1).strip()
    return title, summary


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter — it shouldn't pollute search results."""
    return FRONTMATTER_RE.sub("", content, count=1)


_FTS5_TABLE_RE = re.compile(
    r"\bCREATE\s+VIRTUAL\s+TABLE\b.*\bUSING\s+fts5\s*\(", re.IGNORECASE | re.DOTALL
)
_SEARCH_INDEX_MARKERS = (
    "path unindexed",
    "project unindexed",
    "timestamp unindexed",
    "tokenize = 'porter unicode61'",
)


def _index_schema_is_current(columns: tuple[str, ...], schema_sql: str) -> bool:
    """The live index must have today's columns and today's FTS5 declaration."""
    if columns != SEARCH_INDEX_COLUMNS:
        return False
    if _FTS5_TABLE_RE.search(schema_sql) is None:
        return False
    normalized = " ".join(schema_sql.casefold().split())
    return all(marker in normalized for marker in _SEARCH_INDEX_MARKERS)


def _index_state(
    current_index: Path, deadline: float | None, cancelled: Callable[[], bool] | None
) -> tuple[tuple[str, ...], str, list[str]] | None:
    """(columns, schema SQL, manifest paths) read from the live index."""
    connect_options: dict[str, object] = {}
    if deadline is not None:
        connect_options["timeout"] = max(0.0, min(5.0, deadline - time.monotonic()))
    with closing(sqlite3.connect(str(current_index), **connect_options)) as conn:
        with _legacy_sqlite_guard(conn, deadline, cancelled):
            columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(pages)"))
            schema_row = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                ("pages",),
            ).fetchone()
            manifest_row = conn.execute(
                "SELECT value FROM index_metadata WHERE key = 'paths'"
            ).fetchone()
    if manifest_row is None:
        return None
    schema_sql = schema_row[0] if schema_row and isinstance(schema_row[0], str) else ""
    return columns, schema_sql, json.loads(manifest_row[0])


def _any_page_newer_than(
    pages: list[Path],
    index_mtime: float,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    for page in pages:
        _check_legacy_stop(deadline, cancelled)
        try:
            if page.stat().st_mtime > index_mtime:
                return True
        except OSError:
            continue
    return False


def _needs_rebuild(
    pages: list[Path],
    *,
    root: Path | None = None,
    index_file: Path | None = None,
    index_manifest: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """True when a page is newer than the index, or membership changed."""
    _check_legacy_stop(deadline, cancelled)
    source_root = root or ROOT
    current_index = index_file or INDEX_FILE
    if not current_index.exists():
        return True
    if index_file is None and index_manifest is None:
        with _index_swap_lock(deadline=deadline, cancelled=cancelled):
            return _needs_rebuild(
                pages,
                root=source_root,
                index_file=current_index,
                index_manifest=INDEX_MANIFEST,
                deadline=deadline,
                cancelled=cancelled,
            )
    try:
        state = _index_state(current_index, deadline, cancelled)
    except sqlite3.Error:
        return True
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if state is None:
        return True
    columns, schema_sql, manifest_paths = state
    if not _index_schema_is_current(columns, schema_sql):
        return True
    # The manifest lives inside the SQLite candidate, so index and source
    # membership become active through the same atomic file replacement.
    _check_legacy_stop(deadline, cancelled)
    current_paths = sorted(page.relative_to(source_root).as_posix() for page in pages)
    if manifest_paths != current_paths:
        return True
    _check_legacy_stop(deadline, cancelled)
    return _any_page_newer_than(
        pages, current_index.stat().st_mtime, deadline, cancelled
    )


def _is_transient_windows_access_error(error: OSError) -> bool:
    return sys.platform == "win32" and isinstance(error, PermissionError) and (
        getattr(error, "winerror", None) in {5, 32, 33}
        or error.errno == 13
    )


def _write_lock_claim(lock_file: Path, payload: bytes) -> bool:
    """Create the lock exclusively and write the claim, or report it is taken."""
    try:
        descriptor = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except PermissionError as error:
        if not _is_transient_windows_access_error(error):
            raise
        return False
    try:
        os.write(descriptor, payload)
    except BaseException:
        os.close(descriptor)
        try:
            lock_file.unlink()
        except OSError:
            pass
        raise
    os.close(descriptor)
    return True


def _lock_owner_alive(lock_file: Path) -> bool:
    """A malformed or unreadable claim counts as abandoned."""
    try:
        owner = json.loads(lock_file.read_text(encoding="utf-8"))
        owner_pid = owner.get("pid")
        owner_token = owner.get("token")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    if not isinstance(owner_pid, int) or not isinstance(owner_token, str) or not owner_token:
        return False
    return bool(_is_pid_alive(owner_pid))


def _lock_age_seconds(lock_file: Path) -> float:
    try:
        return time.time() - lock_file.stat().st_mtime
    except OSError:
        return 0.0


def _clear_stale_lock(lock_file: Path) -> bool:
    """Remove a lock whose owner is gone; report whether it is now free."""
    if _lock_age_seconds(lock_file) < _INDEX_SWAP_STALE_SECONDS:
        return False
    if _lock_owner_alive(lock_file):
        return False
    try:
        lock_file.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _acquire_index_swap_lock(
    lock_file: Path,
    payload: bytes,
    *,
    end: float,
    poll: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    while True:
        _check_legacy_stop(end, cancelled)
        if _write_lock_claim(lock_file, payload):
            return
        if _clear_stale_lock(lock_file):
            continue
        _check_legacy_stop(end, cancelled)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Could not acquire index swap lock: {lock_file}")
        time.sleep(min(poll, remaining))
        _check_legacy_stop(end, cancelled)


def _release_index_swap_lock(lock_file: Path, token: str) -> None:
    """Release only our own claim; another owner's lock is not ours to remove."""
    try:
        owner = json.loads(lock_file.read_text(encoding="utf-8"))
        if owner.get("token") == token:
            lock_file.unlink()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass


@contextmanager
def _index_swap_lock(
    timeout: float = _INDEX_SWAP_WAIT_SECONDS,
    poll: float = 0.01,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Serialize live-index swaps across processes with crash recovery."""
    lock_file = INDEX_FILE.with_suffix(INDEX_FILE.suffix + ".swap.lock")
    token = uuid.uuid4().hex
    payload = json.dumps({"pid": os.getpid(), "token": token}).encode("utf-8")
    end = time.monotonic() + timeout if deadline is None else deadline
    _acquire_index_swap_lock(
        lock_file, payload, end=end, poll=poll, cancelled=cancelled
    )
    try:
        yield
    finally:
        _release_index_swap_lock(lock_file, token)


def _replace_live_index(
    tmp_file: Path,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    end = (
        time.monotonic() + _INDEX_REPLACE_WAIT_SECONDS
        if deadline is None
        else deadline
    )
    attempt = 0
    while True:
        _check_legacy_stop(end, cancelled)
        try:
            os.replace(str(tmp_file), str(INDEX_FILE))
            return
        except PermissionError as error:
            if not _is_transient_windows_access_error(error):
                raise
            _check_legacy_stop(end, cancelled)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise
            delay = min(0.01 * (2 ** min(attempt, 4)), 0.1, remaining)
            time.sleep(delay)
            _check_legacy_stop(end, cancelled)
            attempt += 1


_SEARCH_INDEX_DDL = """
    CREATE VIRTUAL TABLE pages USING fts5(
        path UNINDEXED,
        title,
        summary,
        body,
        project UNINDEXED,
        timestamp UNINDEXED,
        slug,
        tokenize = 'porter unicode61'
    )
    """


def _create_index_schema(conn: sqlite3.Connection, manifest_paths: list[str]) -> None:
    conn.execute(_SEARCH_INDEX_DDL)
    conn.execute(
        "CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) "
        "WITHOUT ROWID"
    )
    conn.execute(
        "INSERT INTO index_metadata (key, value) VALUES ('paths', ?)",
        (json.dumps(manifest_paths, separators=(",", ":")),),
    )


def _index_one_page(conn: sqlite3.Connection, page: Path) -> str | None:
    """Insert one page and return the digest of the bytes that were indexed."""
    try:
        source = read_stable_bytes(page, MAX_PAGE_BYTES, label="search page")
    except (OSError, ValueError):
        return None
    content = source.decode("utf-8", errors="ignore")
    title, summary = _extract_title_and_summary(content, page.stem)
    timestamp = (_extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or "")[:10]
    conn.execute(
        "INSERT INTO pages (path, title, summary, body, project, timestamp, slug) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            page.relative_to(ROOT).as_posix(),
            title,
            summary,
            _strip_frontmatter(content),
            (_extract_frontmatter_field(content, PROJECT_FIELD_RE) or "").lower(),
            timestamp,
            page.stem.replace("-", " ").replace("_", " "),
        ),
    )
    return hashlib.sha256(source).hexdigest()


def _sources_unchanged(
    source_digests: Mapping[Path, str],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """A page edited while the index was building invalidates this build."""
    for path, expected_digest in source_digests.items():
        _check_legacy_stop(deadline, cancelled)
        try:
            current = read_stable_bytes(
                path, MAX_PAGE_BYTES, label="search page publication"
            )
        except (OSError, ValueError):
            return False
        if hashlib.sha256(current).hexdigest() != expected_digest:
            return False
    return True


def _build_index_candidate(
    tmp_file: Path,
    pages: list[Path],
    manifest_paths: list[str],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[Path, str]:
    """Fill a fresh index file and report the digests it was built from."""
    source_digests: dict[Path, str] = {}
    conn = sqlite3.connect(str(tmp_file))
    try:
        _create_index_schema(conn, manifest_paths)
        for page in pages:
            _check_legacy_stop(deadline, cancelled)
            digest = _index_one_page(conn, page)
            if digest is not None:
                source_digests[page] = digest
        conn.commit()
        _check_legacy_stop(deadline, cancelled)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - a close failure must not mask the build
            pass
    return source_digests


def _publish_index_candidate(
    tmp_file: Path,
    source_digests: Mapping[Path, str],
    manifest_paths: list[str],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Builders work independently, then briefly serialise the atomic swap."""
    with _index_swap_lock(deadline=deadline, cancelled=cancelled):
        if not _sources_unchanged(source_digests, deadline, cancelled):
            return
        _replace_live_index(tmp_file, deadline=deadline, cancelled=cancelled)
        try:
            atomic_write(INDEX_MANIFEST, json.dumps(manifest_paths))
        except OSError:
            pass  # best-effort: the manifest inside the index is authoritative


def _build_index(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Build the FTS5 index from scratch into a temporary file, then swap it in.

    Concurrent searches never see a partially built index or a window with no
    index at all.
    """
    _check_legacy_stop(deadline, cancelled)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{INDEX_FILE.name}.", suffix=".tmp", dir=INDEX_DIR
    )
    os.close(descriptor)
    tmp_file = Path(tmp_name)
    manifest_paths = sorted(page.relative_to(ROOT).as_posix() for page in pages)
    try:
        source_digests = _build_index_candidate(
            tmp_file, pages, manifest_paths, deadline=deadline, cancelled=cancelled
        )
        _publish_index_candidate(
            tmp_file,
            source_digests,
            manifest_paths,
            deadline=deadline,
            cancelled=cancelled,
        )
    finally:
        try:
            tmp_file.unlink()
        except FileNotFoundError:
            pass


def _authority_weight(path: str) -> float:
    """Read source_authority from page frontmatter; default 1.0."""
    try:
        p = ROOT / path if not Path(path).is_absolute() else Path(path)
        content = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 1.0
    return authority_weight(_extract_frontmatter_field(content, AUTHORITY_FIELD_RE))


def _valid_as_of(path: str, as_of: str) -> bool:
    """True if page is valid at as_of (valid_to empty/null or >= as_of)."""
    try:
        p = ROOT / path if not Path(path).is_absolute() else Path(path)
        content = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    valid_to = _extract_frontmatter_field(content, VALID_TO_FIELD_RE)
    if not valid_to:
        return True
    vt = valid_to.strip().strip("\"'").lower()
    if vt in ("null", "none", "~", ""):
        return True
    return vt[:10] >= as_of[:10]


def _deduplicate_by_slug(results: list[dict]) -> list[dict]:
    """Remove results with duplicate filename stems, keeping the first (highest-ranked).

    Some pages exist both flat (knowledge/notes/X.md) and under subdirectories
    (knowledge/notes/qa/X.md). This deduplication keeps only the first
    occurrence, preventing the same content from appearing twice.
    """
    seen_stems: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        # Use slug as identifier; fall back to filename stem from path.
        stem = r.get("slug") or Path(r.get("path", "")).stem
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        deduped.append(r)
    return deduped


def _maybe_rerank(query: str, results: list[dict], limit: int) -> list[dict]:
    """Apply cross-encoder reranker if available, else return results as-is."""
    if not results or len(results) <= 1:
        return results[:limit]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from reranker import rerank, should_rerank

        apply, _reason = should_rerank(
            profile=str(results[0].get("requested_mode") or "HYBRID"),
            candidates=results,
            analysis_intents=(),
            rerank_enabled=True,
        )
        if apply:
            results = rerank(query, results, limit=max(limit, len(results)))
    except Exception:
        pass

    results = _deduplicate_by_slug(results)

    return results[:limit]


def _finalize_results(
    query: str,
    results: list[dict],
    limit: int,
    *,
    retrieval_mode: str,
    source_tool: str,
    emit_telemetry: bool,
) -> list[dict]:
    """Finalize one returned list and best-effort record its impressions once."""
    final = _deduplicate_by_slug(results)[:limit]
    mode = str(retrieval_mode or "bm25").lower()
    for result in final:
        result.setdefault("generation", "legacy")
        result["requested_mode"] = mode
        result["effective_mode"] = mode
        result.setdefault("fallback_reason", None)
    if not final or not emit_telemetry:
        return final
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = []
        for rank, result in enumerate(final, start=1):
            candidate_id = result.get("slug") or Path(result.get("path", "")).stem
            event = best_effort_make_event(
                event_kind="impression",
                query=query,
                retrieval_mode=mode,
                candidate_id=candidate_id,
                rank=rank,
                generation=str(result.get("generation") or "legacy"),
                source_tool=source_tool,
            )
            if event is not None:
                events.append(event)
        if events:
            best_effort_record_events(events)
    except Exception:
        pass
    return final


def _active_generation_catalog() -> GenerationCatalog | None:
    catalog_path = STATE_ROOT / "cache/evidence-graph/catalog.sqlite3"
    if not catalog_path.exists():
        return None
    try:
        return GenerationCatalog(STATE_ROOT, catalog_path=catalog_path)
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        return None


def _generation_artifact(manifest: dict[str, object], name: str) -> bool:
    artifacts = manifest.get("artifacts")
    return isinstance(artifacts, list) and any(
        isinstance(artifact, dict) and artifact.get("path") == name
        for artifact in artifacts
    )


def _check_generation_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("generation retrieval cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("generation retrieval deadline exceeded")


@contextmanager
def _generation_sqlite_guard(
    connection: sqlite3.Connection,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[None]:
    _check_generation_stop(deadline, cancelled)
    if deadline is None and cancelled is None:
        yield
        return
    stopped = False

    def progress() -> int:
        nonlocal stopped
        stopped = bool(cancelled and cancelled()) or bool(
            deadline is not None and time.monotonic() >= deadline
        )
        return int(stopped)

    connection.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.DatabaseError as exc:
        if stopped:
            raise TimeoutError(
                "generation SQLite work cancelled or deadline exceeded"
            ) from exc
        _check_generation_stop(deadline, cancelled)
        raise
    finally:
        connection.set_progress_handler(None, 0)
    _check_generation_stop(deadline, cancelled)


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sealed_file(
    path: Path,
    expected: dict[str, object] | None = None,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple:
    _check_generation_stop(deadline, cancelled)
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise PermissionError("generation artifact must be a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if not os.path.samestat(before, opened):
            raise PermissionError("generation artifact changed while opening")
        while chunk := source.read(64 * 1024):
            _check_generation_stop(deadline, cancelled)
            size += len(chunk)
            digest.update(chunk)
        _check_generation_stop(deadline, cancelled)
        after_open = os.fstat(source.fileno())
    after = path.lstat()
    if (
        not os.path.samestat(opened, after_open)
        or not os.path.samestat(after_open, after)
        or (opened.st_size, opened.st_mtime_ns)
        != (after_open.st_size, after_open.st_mtime_ns)
    ):
        raise PermissionError("generation artifact changed while hashing")
    checksum = digest.hexdigest()
    if expected is not None and (
        expected.get("size") != size or expected.get("sha256") != checksum
    ):
        raise ValueError("generation artifact does not match active manifest")
    return (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        checksum,
    )


def _generation_consumption_seal(
    catalog: object,
    manifest: dict[str, object],
    artifact_names: tuple[str, ...],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple | None:
    try:
        _check_generation_stop(deadline, cancelled)
        generation_id = manifest["generation_id"]
        generations_path = Path(getattr(catalog, "generations_path"))
        if not isinstance(generation_id, str):
            return None
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            return None
        descriptors = {
            item.get("path"): item
            for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if len(descriptors) != len(artifacts):
            return None
        directory = generations_path / generation_id
        seals = []
        for name in artifact_names:
            _check_generation_stop(deadline, cancelled)
            descriptor = descriptors.get(name)
            if not isinstance(descriptor, dict):
                return None
            seals.append(
                (
                    name,
                    _sealed_file(
                        directory / name,
                        descriptor,
                        deadline=deadline,
                        cancelled=cancelled,
                    ),
                )
            )
        manifest_path = directory / "manifest.json"
        canonical = _canonical_manifest_bytes(manifest)
        if manifest_path.exists() or manifest_path.is_symlink():
            manifest_seal = _sealed_file(
                manifest_path,
                {
                    "size": len(canonical),
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                },
                deadline=deadline,
                cancelled=cancelled,
            )
        else:
            manifest_seal = None
        return hashlib.sha256(canonical).hexdigest(), manifest_seal, tuple(seals)
    except TimeoutError:
        raise
    except (OSError, PermissionError, TypeError, ValueError):
        return None


def _generation_consumption_unchanged(
    catalog: object,
    manifest: dict[str, object],
    artifact_names: tuple[str, ...],
    expected_seal: tuple,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    try:
        from repository_scope import RepositoryScope

        _check_generation_stop(deadline, cancelled)
        repository_scope = RepositoryScope.from_dict(manifest.get("repository_scope"))
        if deadline is None and cancelled is None:
            active = catalog.get_active_for_repository(repository_scope)
        else:
            active = catalog.get_active_for_repository(
                repository_scope, deadline=deadline, cancelled=cancelled
            )
        return (
            isinstance(active, dict)
            and _canonical_manifest_bytes(active) == _canonical_manifest_bytes(manifest)
            and _generation_consumption_seal(
                catalog,
                active,
                artifact_names,
                deadline=deadline,
                cancelled=cancelled,
            )
            == expected_seal
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        return False


def _generation_connection(
    catalog: object,
    manifest: dict[str, object],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> sqlite3.Connection | None:
    _check_generation_stop(deadline, cancelled)
    generation_id = manifest.get("generation_id")
    generations_path = getattr(catalog, "generations_path", None)
    if not isinstance(generation_id, str) or generations_path is None:
        return None
    if not _generation_artifact(manifest, GENERATION_FTS_ARTIFACT):
        return None
    artifact = Path(generations_path) / generation_id / GENERATION_FTS_ARTIFACT
    try:
        connect_options = {"uri": True}
        if deadline is not None:
            connect_options["timeout"] = max(
                0.0, min(5.0, deadline - time.monotonic())
            )
        _check_generation_stop(deadline, cancelled)
        connection = sqlite3.connect(
            f"{artifact.resolve().as_uri()}?mode=ro", **connect_options
        )
        _check_generation_stop(deadline, cancelled)
        if not _valid_generation_fts(
            connection, manifest, deadline=deadline, cancelled=cancelled
        ):
            connection.close()
            return None
        return connection
    except TimeoutError:
        try:
            connection.close()
        except (UnboundLocalError, sqlite3.Error):
            pass
        raise
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        try:
            connection.close()
        except (UnboundLocalError, sqlite3.Error):
            pass
        return None


def _valid_generation_fts(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    with _generation_sqlite_guard(connection, deadline, cancelled):
        return _valid_generation_fts_contents(
            connection,
            manifest,
            deadline=deadline,
            cancelled=cancelled,
            authoritative_sources=authoritative_sources,
        )


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
        chunks: list[bytes] = []
        total = 0
        while True:
            _check_generation_stop(deadline, cancelled)
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds its byte ceiling")
            chunks.append(chunk)
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


def _validated_source_manifest(
    generation_path: Path,
    manifest: Mapping[str, object],
    *,
    state_root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict:
    source_manifest_path = generation_path / "source-manifest.json"
    expected = validate_runtime_file(
        source_manifest_path, state_root, max_bytes=MAX_CORPUS_TOTAL_BYTES
    )
    raw = _read_identity_stable_bytes(
        source_manifest_path,
        expected,
        max_bytes=MAX_CORPUS_TOTAL_BYTES,
        label="generation source manifest",
        deadline=deadline,
        cancelled=cancelled,
    )
    _check_generation_stop(deadline, cancelled)
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


def _valid_source_row(row: tuple[object, ...], seen: set[str]) -> bool:
    source_id, relative_path, digest, size, content_size = row
    if not isinstance(source_id, str) or source_id in seen:
        return False
    if not isinstance(relative_path, str) or not isinstance(digest, str):
        return False
    if not isinstance(size, int) or not isinstance(content_size, int):
        return False
    return content_size <= MAX_CORPUS_FILE_BYTES and size == content_size


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
    rows = database.execute(
        "SELECT source_id, relative_path, sha256, size, length(content) FROM source "
        "ORDER BY relative_path, source_id LIMIT ?",
        (MAX_CORPUS_FILES + 1,),
    )
    for row in rows:
        _check_generation_stop(deadline, cancelled)
        if len(metadata) >= MAX_CORPUS_FILES:
            raise ValueError("generation source row ceiling exceeded")
        if not _valid_source_row(row, seen):
            raise ValueError("generation evidence source rows are invalid")
        source_id, relative_path, digest, size, content_size = row
        total_bytes += content_size
        if total_bytes > MAX_CORPUS_TOTAL_BYTES:
            raise ValueError("generation evidence source bytes exceed their ceiling")
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
        _check_generation_stop(deadline, cancelled)
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


def _generation_authoritative_sources(
    generation_path: Path,
    manifest: dict[str, object],
    *,
    state_root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, object]]:
    """Every source the generation claims, read back and verified byte for byte."""
    _check_generation_stop(deadline, cancelled)
    source_manifest = _validated_source_manifest(
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
        with _generation_sqlite_guard(database, deadline, cancelled):
            metadata = _source_metadata_rows(
                database, deadline=deadline, cancelled=cancelled
            )
            sources = _verified_source_contents(
                database, metadata, deadline=deadline, cancelled=cancelled
            )
    _require_membership_matches(sources, source_manifest)
    return sources


def validate_generation_fts_artifact(
    generation_path: Path,
    manifest: dict[str, object],
    *,
    state_root: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Fail closed unless a generation-local FTS artifact is semantically valid."""
    _check_generation_stop(deadline, cancelled)
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    authoritative_sources = _generation_authoritative_sources(
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
        if not _valid_generation_fts(
            connection,
            manifest,
            deadline=deadline,
            cancelled=cancelled,
            authoritative_sources=authoritative_sources,
        ):
            raise ValueError("generation FTS search artifact is semantically invalid")
    _check_generation_stop(deadline, cancelled)


class _UnusableAuthoritativeSource(Exception):
    """An authoritative source that cannot produce chunks at all."""


_FTS_METADATA_SCHEMA = (
    ("key", "TEXT", 0, 1),
    ("value", "TEXT", 1, 0),
)
_FTS_TABLE_PREFIX = "create virtual table chunks using fts5("
_FTS_CHUNK_SELECT = (
    "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
    "parent_page, heading_ancestry, byte_start, byte_end, line_start, line_end, "
    "span_sha256, type, project, authority, confidence, status, valid_from, "
    "valid_to, language, title, content FROM chunks ORDER BY chunk_order"
)


def _valid_table_shapes(connection: sqlite3.Connection) -> bool:
    metadata_schema = tuple(
        (row[1], row[2].upper(), row[3], row[5])
        for row in connection.execute("PRAGMA table_info(generation_metadata)")
    )
    chunk_columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(chunks)"))
    return metadata_schema == _FTS_METADATA_SCHEMA and chunk_columns == GENERATION_FTS_COLUMNS


def _valid_fts_schema(connection: sqlite3.Connection) -> bool:
    """Integrity, table shapes, and the exact FTS5 declaration."""
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        return False
    if not _valid_table_shapes(connection):
        return False
    return _valid_fts_declaration(connection)


def _expected_fts_arguments() -> list[str]:
    return [
        *(f"{column} unindexed" for column in GENERATION_FTS_COLUMNS[:-2]),
        "title",
        "content",
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


def _valid_fts_declaration(connection: sqlite3.Connection) -> bool:
    return _declared_fts_arguments(connection) == _expected_fts_arguments()


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
    expected = {
        "schema_version": GENERATION_SEARCH_SCHEMA_VERSION,
        "collector_version": manifest.get("collector_version"),
        "extractor_version": manifest.get("extractor_version"),
        "tokenizer_version": GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": GENERATION_TOKENIZER_CONFIG_SHA256,
        "source_manifest_sha256": manifest.get("source_manifest_sha256"),
    }
    return all(metadata.get(key) == value for key, value in expected.items())


def _expected_source_chunks(
    source_id: str,
    source: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[object]:
    content = source["content"]
    if not isinstance(content, bytes):
        raise _UnusableAuthoritativeSource
    yield from canonical_retrieval_chunks(
        source_id=source_id,
        source_path=str(source["relative_path"]),
        source_sha256=str(source["sha256"]),
        content=content,
        extractor_version=str(manifest.get("extractor_version")),
        deadline=deadline,
        cancelled=cancelled,
    )


def _expected_chunk_rows(
    authoritative_sources: Mapping[str, Mapping[str, object]],
    *,
    manifest: Mapping[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[tuple[object, ...]] | None:
    """Every chunk the authoritative sources must produce, in stored order."""
    ordered = sorted(
        authoritative_sources.items(),
        key=lambda item: (str(item[1]["relative_path"]), item[0]),
    )
    rows: list[tuple[object, ...]] = []
    for source_id, source in ordered:
        _check_generation_stop(deadline, cancelled)
        try:
            chunks = _expected_source_chunks(
                source_id,
                source,
                manifest=manifest,
                deadline=deadline,
                cancelled=cancelled,
            )
            for chunk in chunks:
                rows.append(_generation_chunk_row(chunk, len(rows)))
                if len(rows) > MAX_GENERATION_FTS_CHUNKS:
                    return None
        except _UnusableAuthoritativeSource:
            return None
    return rows


def _stored_chunk_count(connection: sqlite3.Connection) -> int:
    return len(
        list(
            connection.execute(
                "SELECT 1 FROM chunks LIMIT ?", (MAX_GENERATION_FTS_CHUNKS + 1,)
            )
        )
    )


def _chunk_count_agrees(
    count: int, metadata: Mapping[str, str], expected_chunks: list[tuple[object, ...]] | None
) -> bool:
    if count > MAX_GENERATION_FTS_CHUNKS:
        return False
    if metadata.get("chunk_count") != str(count):
        return False
    return expected_chunks is None or count == len(expected_chunks)


def _valid_chunk_identity(row: tuple[object, ...], order: int, seen: set[str]) -> bool:
    chunk_id, chunk_order, source_id, source_path, source_sha256, parent_page = row[:6]
    if not isinstance(chunk_id, str) or _SHA256_RE.fullmatch(chunk_id) is None:
        return False
    if chunk_id in seen or chunk_order != order:
        return False
    if not isinstance(source_path, str) or not source_path:
        return False
    if source_id != f"source:{source_path}" or parent_page != source_path:
        return False
    return isinstance(source_sha256, str) and _SHA256_RE.fullmatch(source_sha256) is not None


def _valid_chunk_span(row: tuple[object, ...], ancestry: object) -> bool:
    _, _, _, _, _, _, _, byte_start, byte_end, line_start, line_end, span_sha256 = row[:12]
    if not isinstance(ancestry, list) or any(not isinstance(item, str) for item in ancestry):
        return False
    if not isinstance(byte_start, int) or not isinstance(byte_end, int):
        return False
    if not 0 <= byte_start <= byte_end:
        return False
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        return False
    if not 1 <= line_start <= line_end:
        return False
    return isinstance(span_sha256, str) and _SHA256_RE.fullmatch(span_sha256) is not None


def _valid_chunk_text(row: tuple[object, ...]) -> bool:
    type_value = row[12]
    optional_text = (row[13], row[14], row[15], row[17], row[18], row[19])
    status_value, title, content = row[16], row[20], row[21]
    if not isinstance(type_value, str) or not type_value:
        return False
    if any(value is not None and not isinstance(value, str) for value in optional_text):
        return False
    if not isinstance(status_value, str) or not isinstance(title, str):
        return False
    return isinstance(content, str) and bool(content.strip())


def _valid_stored_chunk(row: tuple[object, ...], order: int, seen: set[str]) -> bool:
    try:
        ancestry = json.loads(row[6])
    except (TypeError, json.JSONDecodeError):
        return False
    if not _valid_chunk_identity(row, order, seen):
        return False
    if not _valid_chunk_span(row, ancestry):
        return False
    return _valid_chunk_text(row)


def _stored_chunks_match(
    connection: sqlite3.Connection,
    expected_chunks: list[tuple[object, ...]] | None,
    *,
    count: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    seen: set[str] = set()
    for order, row in enumerate(connection.execute(_FTS_CHUNK_SELECT)):
        _check_generation_stop(deadline, cancelled)
        if not _valid_stored_chunk(row, order, seen):
            return False
        if expected_chunks is not None and order < len(expected_chunks):
            if row != expected_chunks[order]:
                return False
        seen.add(str(row[0]))
    return len(seen) == count


def _valid_generation_fts_contents(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    """Every structural claim the FTS artifact makes about itself must hold."""
    if not _valid_fts_schema(connection):
        return False
    metadata = _generation_metadata(connection)
    if metadata is None or not _metadata_matches_manifest(metadata, manifest):
        return False
    expected_chunks = None
    if authoritative_sources is not None:
        expected_chunks = _expected_chunk_rows(
            authoritative_sources,
            manifest=manifest,
            deadline=deadline,
            cancelled=cancelled,
        )
        if expected_chunks is None:
            return False
    count = _stored_chunk_count(connection)
    if not _chunk_count_agrees(count, metadata, expected_chunks):
        return False
    return _stored_chunks_match(
        connection,
        expected_chunks,
        count=count,
        deadline=deadline,
        cancelled=cancelled,
    )


def _fts_query(query: str) -> str:
    return " ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in query.split() if word)


def _normalized_filename_stem(value: str) -> str:
    name = Path(value.strip()).name.casefold()
    if name.endswith(".md"):
        name = name[:-3]
    return "-".join(part for part in re.split(r"[\s_-]+", name) if part)


def _generation_filters(
    *, scope: str, since: str | None, as_of: str | None
) -> tuple[str, list[str]]:
    clauses = []
    values: list[str] = []
    if scope in {"wiki", "memory", "knowledge"}:
        clauses.append("source_path LIKE 'knowledge/notes/%'")
    if as_of:
        clauses.extend(
            (
                "(valid_from IS NULL OR valid_from = '' OR substr(valid_from, 1, 10) <= ?)",
                "(valid_to IS NULL OR valid_to = '' OR substr(valid_to, 1, 10) > ?)",
            )
        )
        values.extend((as_of[:10], as_of[:10]))
    else:
        clauses.append("(status IS NULL OR status = '' OR lower(status) = 'active')")
    if since:
        clauses.append(
            "(valid_from IS NULL OR valid_from = '' OR substr(valid_from, 1, 10) >= ?)"
        )
        values.append(since[:10])
    return (" AND " + " AND ".join(clauses) if clauses else ""), values


_NOTES_SCOPES = frozenset({"wiki", "memory", "knowledge"})
_OPEN_ENDED_VALID_TO = frozenset({"", "null", "none"})


def _row_path(row: Mapping[str, object]) -> str:
    return str(row.get("path") or row.get("relative_path") or "")


def _out_of_scope(row: Mapping[str, object], scope: str) -> bool:
    """Notes-only scopes keep pages under knowledge/notes."""
    if scope not in _NOTES_SCOPES:
        return False
    path = _row_path(row)
    if not path:
        return False
    return "knowledge/notes/" not in path.replace("\\", "/")


def _field_mismatch(row: Mapping[str, object], field: str, wanted: str | None) -> bool:
    if not wanted:
        return False
    return str(row.get(field) or "").casefold() != wanted.casefold()


def _before_since(timestamp: str, since: str | None) -> bool:
    if not since or not timestamp:
        return False
    return timestamp[:10] < since[:10]


def _after(day: str, value: str) -> bool:
    return bool(value) and value[:10] > day


def _expired_by(row: Mapping[str, object], day: str) -> bool:
    valid_to = str(row.get("valid_to") or "")[:10]
    if not valid_to or valid_to in _OPEN_ENDED_VALID_TO:
        return False
    return valid_to <= day


def _outside_as_of(row: Mapping[str, object], timestamp: str, as_of: str) -> bool:
    """A page is out of scope when it started later or stopped being true."""
    day = as_of[:10]
    if _after(day, timestamp) or _after(day, str(row.get("valid_from") or "")):
        return True
    return _expired_by(row, day)


def _superseded_now(row: Mapping[str, object]) -> bool:
    """Without an as-of date only currently active pages answer."""
    status = str(row.get("status") or "").casefold()
    return bool(status) and status != "active"


def _temporally_excluded(row: Mapping[str, object], since: str | None, as_of: str | None) -> bool:
    timestamp = str(row.get("timestamp") or row.get("valid_from") or "")
    if _before_since(timestamp, since):
        return True
    if as_of:
        return _outside_as_of(row, timestamp, as_of)
    return _superseded_now(row)


def _passes_hard_filters(
    row: Mapping[str, object],
    *,
    project: str | None,
    since: str | None,
    as_of: str | None,
    scope: str,
    authority: str | None,
) -> bool:
    if _out_of_scope(row, scope):
        return False
    if _field_mismatch(row, "project", project):
        return False
    if _field_mismatch(row, "authority", authority):
        return False
    return not _temporally_excluded(row, since, as_of)


def _title_boost(title: str, query_lower: str, query_words: set[str]) -> float:
    """A title that is the query is the strongest lexical signal there is."""
    title_lower = (title or "").lower().strip()
    title_words = set(title_lower.split())
    if title_lower == query_lower:
        return 5.0
    if query_words and query_words.issubset(title_words):
        return 3.0
    if title_words and title_words.issubset(query_words):
        return 2.0
    return 1.0


def _filename_boost(path: str, query_lower: str, query_words: set[str]) -> float:
    slug = Path(path).stem.lower().replace("-", " ")
    if slug == query_lower:
        return 10.0
    if query_words and query_words.issubset(set(slug.split())):
        return 4.0
    return 1.0


def _project_boost(row_project: str, project: str | None) -> float:
    if project and row_project and row_project.lower() == project.lower():
        return 2.0
    return 1.0


def _notes_boost(path: str) -> float:
    """knowledge/notes/ is the canonical durable tree; ties break toward it."""
    if "knowledge/notes/" in path:
        return 1.3
    return 1.0


def _boosted_lexical_score(
    rank: float,
    *,
    path: str,
    title: str,
    row_project: str,
    query_lower: str,
    query_words: set[str],
    project: str | None,
) -> float:
    """One lexical scoring ladder, shared by the generation and legacy paths."""
    score = -rank
    score *= _project_boost(row_project, project)
    score *= _title_boost(title, query_lower, query_words)
    score *= _filename_boost(path, query_lower, query_words)
    score *= _notes_boost(path)
    return score * _authority_weight(path)


def apply_hard_filters(
    rows: list[dict],
    *,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    scope: str = "all",
    authority: str | None = None,
    **_ignored: object,
) -> list[dict]:
    """Single hard-filter contract shared by lexical / NumPy / Lance paths."""
    return [
        row
        for row in rows
        if _passes_hard_filters(
            row,
            project=project,
            since=since,
            as_of=as_of,
            scope=scope,
            authority=authority,
        )
    ]


def _generation_result(row: sqlite3.Row, generation_id: str) -> dict[str, object]:
    score = -float(row["rank"])
    authority = row["authority"] or ""
    score *= authority_weight(authority)
    content = row["content"] or ""
    return {
        "path": row["source_path"],
        "title": row["title"] or Path(row["source_path"]).stem,
        "summary": content.strip().splitlines()[0][:120] if content.strip() else "",
        "content": content,
        "score": score,
        "project": row["project"] or "",
        "timestamp": (row["valid_from"] or "")[:10],
        "chunk_id": row["chunk_id"],
        "candidate_id": row["chunk_id"],
        "source_id": row["source_id"],
        "source_sha256": row["source_sha256"],
        "heading_ancestry": json.loads(row["heading_ancestry"]),
        "type": row["type"],
        "authority": authority,
        "confidence": row["confidence"] or "",
        "status": row["status"] or "",
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "language": row["language"],
        "generation": generation_id,
        "requested_mode": "base",
        "effective_mode": "base",
        "fallback_reason": None,
        "_chunk_order": row["chunk_order"],
    }


_GENERATION_CHUNK_COLUMNS = (
    "chunk_id, chunk_order, source_id, source_path, source_sha256, "
    "heading_ancestry, type, project, authority, confidence, status, valid_from, "
    "valid_to, language, title, content"
)


def _register_filename_stem_function(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "llm_wiki_filename_stem",
        1,
        lambda value: _normalized_filename_stem(value) if isinstance(value, str) else "",
        deterministic=True,
    )


def _exact_filename_rows(
    connection: sqlite3.Connection,
    normalized_stem: str,
    filters: str,
    values: Sequence[object],
    project: str | None,
) -> list[sqlite3.Row]:
    """The one chunk whose file name is the query, if the corpus holds it."""
    exact_filters = filters
    exact_values = list(values)
    if project:
        exact_filters += " AND lower(project) = lower(?)"
        exact_values.append(project)
    return connection.execute(
        f"SELECT {_GENERATION_CHUNK_COLUMNS}, 0.0 AS rank FROM chunks "
        "WHERE llm_wiki_filename_stem(source_path) = ?"
        f"{exact_filters} ORDER BY source_path, chunk_order LIMIT 1",
        [normalized_stem, *exact_values],
    ).fetchall()


def _generation_matched_rows(
    connection: sqlite3.Connection,
    query: str,
    filters: str,
    values: Sequence[object],
    limit: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT {_GENERATION_CHUNK_COLUMNS}, bm25(chunks) AS rank FROM chunks "
        f"WHERE chunks MATCH ?{filters} ORDER BY rank, chunk_order LIMIT ?",
        [_fts_query(query), *values, limit * 5],
    ).fetchall()


def _deduplicated_results(
    rows: Sequence[sqlite3.Row],
    generation_id: str,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        _check_generation_stop(deadline, cancelled)
        chunk_id = str(row["chunk_id"])
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        results.append(_generation_result(row, generation_id))
    return results


def _boost_generation_results(
    results: list[dict[str, object]],
    query: str,
    project: str | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Project match doubles the score here; a title containing the query triples it."""
    query_words = set(query.casefold().split())
    for result in results:
        _check_generation_stop(deadline, cancelled)
        score = float(result["score"])
        if project and str(result["project"]).casefold() == project.casefold():
            score *= 2.0
        title_words = set(str(result["title"]).casefold().split())
        if query_words and query_words.issubset(title_words):
            score *= 3.0
        result["score"] = score


def _generation_fts_search(
    query: str,
    manifest: dict[str, object],
    connection: sqlite3.Connection,
    *,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    """BM25 over one generation, with an exact filename match kept in front."""
    with _generation_sqlite_guard(connection, deadline, cancelled):
        connection.row_factory = sqlite3.Row
        filters, values = _generation_filters(scope=scope, since=since, as_of=as_of)
        _register_filename_stem_function(connection)
        exact_rows = _exact_filename_rows(
            connection, _normalized_filename_stem(query), filters, values, project
        )
        rows = _generation_matched_rows(connection, query, filters, values, limit)
    exact_path = str(exact_rows[0]["source_path"]) if exact_rows else None
    results = _deduplicated_results(
        [*exact_rows, *rows],
        str(manifest["generation_id"]),
        deadline=deadline,
        cancelled=cancelled,
    )
    _boost_generation_results(
        results, query, project, deadline=deadline, cancelled=cancelled
    )
    results.sort(
        key=lambda item: (
            0 if item["path"] == exact_path else 1,
            -float(item["score"]),
            str(item["path"]),
            int(item["_chunk_order"]),
        )
    )
    for result in results:
        _check_generation_stop(deadline, cancelled)
        result["score"] = round(float(result["score"]), 4)
        result.pop("_chunk_order", None)
    filtered = apply_hard_filters(
        results, project=project, since=since, as_of=as_of, scope=scope
    )
    return filtered[:limit]


def _vectors_match_manifest(
    manifest: Mapping[str, object], model_id: str, model_revision: str
) -> bool:
    """The generation must claim complete vectors from this exact model."""
    if manifest.get("vector_state") != "complete":
        return False
    if manifest.get("embedding_model_id") != model_id:
        return False
    if manifest.get("embedding_model_revision") != model_revision:
        return False
    return all(_generation_artifact(manifest, name) for name in GENERATION_VECTOR_ARTIFACTS)


def _read_vector_metadata(
    directory: Path, deadline: float | None, cancelled: Callable[[], bool] | None
) -> dict:
    metadata_bytes = bytearray()
    with (directory / "vectors.json").open("rb") as source:
        while chunk := source.read(64 * 1024):
            _check_generation_stop(deadline, cancelled)
            metadata_bytes.extend(chunk)
    _check_generation_stop(deadline, cancelled)
    return json.loads(metadata_bytes.decode("utf-8"))


def _matrix_is_finite(
    matrix: object, deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    import numpy as np

    for start in range(0, len(matrix), 4096):
        _check_generation_stop(deadline, cancelled)
        if not np.isfinite(matrix[start : start + 4096]).all():
            return False
    return True


def _vector_metadata_matches(
    metadata: Mapping[str, object],
    manifest: Mapping[str, object],
    ordered: list[sqlite3.Row],
    *,
    model_id: str,
    model_revision: str,
    dimensions: object,
) -> bool:
    """Every claim vectors.json makes about its corpus must hold."""
    expected = {
        "schema_version": "corpus-vectors/v1",
        "corpus_sha256": manifest.get("source_manifest_sha256"),
        "collector_version": manifest.get("collector_version"),
        "extractor_version": manifest.get("extractor_version"),
        "model_id": model_id,
        "model_revision": model_revision,
        "dimensions": dimensions,
        "chunk_ids": [row[0] for row in ordered],
        "source_ids": [row[1] for row in ordered],
        "source_paths": [row[2] for row in ordered],
        "source_sha256": [row[3] for row in ordered],
    }
    return all(metadata.get(key) == value for key, value in expected.items())


def _usable_vector_matrix(
    matrix: object, ordered: list[sqlite3.Row], dimensions: object, *, deadline, cancelled
) -> bool:
    import numpy as np

    if matrix.shape != (len(ordered), dimensions):
        return False
    if matrix.dtype != np.dtype(np.float32):
        return False
    return _matrix_is_finite(matrix, deadline, cancelled)


def _usable_query_vector(query_matrix: object, dimensions: object) -> bool:
    import numpy as np

    if query_matrix.shape != (1, dimensions):
        return False
    if query_matrix.dtype.kind != "f":
        return False
    return bool(np.isfinite(query_matrix).all())


def _cosine_similarities(
    matrix: object, query_vector: object, *, deadline, cancelled
) -> object:
    import numpy as np

    query_norm = np.linalg.norm(query_vector) + 1e-10
    similarities = np.empty(len(matrix), dtype=np.float32)
    for start in range(0, len(matrix), 4096):
        _check_generation_stop(deadline, cancelled)
        block = matrix[start : start + 4096]
        similarities[start : start + len(block)] = (block @ query_vector) / (
            (np.linalg.norm(block, axis=1) + 1e-10) * query_norm
        )
    return similarities


def _ordered_chunk_identity(
    connection: sqlite3.Connection, deadline, cancelled
) -> list[sqlite3.Row]:
    with _generation_sqlite_guard(connection, deadline, cancelled):
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT chunk_id, source_id, source_path, source_sha256 "
            "FROM chunks ORDER BY chunk_order"
        ).fetchall()


def _vector_scored_rows(
    connection: sqlite3.Connection,
    similarities: object,
    generation_id: str,
    *,
    scope: str,
    since: str | None,
    as_of: str | None,
    project: str | None,
    deadline,
    cancelled,
) -> list[dict[str, object]]:
    filters, values = _generation_filters(scope=scope, since=since, as_of=as_of)
    with _generation_sqlite_guard(connection, deadline, cancelled):
        rows = connection.execute(
            "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
            "heading_ancestry, type, project, authority, confidence, status, valid_from, "
            "valid_to, language, title, content, 0.0 AS rank FROM chunks "
            f"WHERE 1=1{filters} ORDER BY chunk_order",
            values,
        ).fetchall()
    results = []
    for row in rows:
        _check_generation_stop(deadline, cancelled)
        result = _generation_result(row, generation_id)
        score = float(similarities[row["chunk_order"]])
        # The vector path boosts a project match by 1.5, not by the lexical 2.0.
        if project and str(result["project"]).casefold() == project.casefold():
            score *= 1.5
        result["score"] = round(score, 4)
        result["requested_mode"] = "hybrid"
        result["effective_mode"] = "hybrid"
        results.append(result)
    results.sort(key=lambda item: (-float(item["score"]), str(item["chunk_id"])))
    return results


def _generation_vectors_search(
    query: str,
    catalog: object,
    manifest: dict[str, object],
    connection: sqlite3.Connection,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, object]] | None:
    """Cosine search over one generation's vectors, or None when unusable."""
    _check_generation_stop(deadline, cancelled)
    if not _vectors_match_manifest(manifest, model_id, model_revision):
        return None
    generation_id = str(manifest["generation_id"])
    directory = Path(getattr(catalog, "generations_path")) / generation_id
    try:
        import numpy as np

        metadata = _read_vector_metadata(directory, deadline, cancelled)
        matrix = np.load(directory / "vectors.npy", mmap_mode="r", allow_pickle=False)
        _check_generation_stop(deadline, cancelled)
        ordered = _ordered_chunk_identity(connection, deadline, cancelled)
        dimensions = manifest.get("vector_dimensions")
        if not _vector_metadata_matches(
            metadata,
            manifest,
            ordered,
            model_id=model_id,
            model_revision=model_revision,
            dimensions=dimensions,
        ):
            return None
        if not _usable_vector_matrix(
            matrix, ordered, dimensions, deadline=deadline, cancelled=cancelled
        ):
            return None
        _check_generation_stop(deadline, cancelled)
        query_matrix = np.asarray(_call_generation_embedder(embedder, [query]))
        _check_generation_stop(deadline, cancelled)
        if not _usable_query_vector(query_matrix, dimensions):
            return None
        similarities = _cosine_similarities(
            matrix, query_matrix[0], deadline=deadline, cancelled=cancelled
        )
        results = _vector_scored_rows(
            connection,
            similarities,
            generation_id,
            scope=scope,
            since=since,
            as_of=as_of,
            project=project,
            deadline=deadline,
            cancelled=cancelled,
        )
        _check_generation_stop(deadline, cancelled)
        return results[: limit * 3]
    except TimeoutError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _fuse_generation_results(
    lexical: list[dict[str, object]], vectors: list[dict[str, object]], limit: int
) -> list[dict[str, object]]:
    scores: dict[str, float] = {}
    metadata: dict[str, dict[str, object]] = {}
    for rank, result in enumerate(lexical, 1):
        chunk_id = str(result["chunk_id"])
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 2.0 / (60 + rank)
        metadata[chunk_id] = result
    for rank, result in enumerate(vectors, 1):
        chunk_id = str(result["chunk_id"])
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        metadata.setdefault(chunk_id, result)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    results = []
    for chunk_id in ordered[:limit]:
        result = dict(metadata[chunk_id])
        result["score"] = round(scores[chunk_id], 4)
        result["requested_mode"] = "hybrid"
        result["effective_mode"] = "hybrid"
        result["fallback_reason"] = None
        results.append(result)
    return results


def _finalize_generation_results(
    results: list[dict[str, object]],
    *,
    query: str,
    source_tool: str,
    emit_telemetry: bool,
) -> list[dict[str, object]]:
    if not results or not emit_telemetry:
        return results
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = []
        for rank, result in enumerate(results, 1):
            event = best_effort_make_event(
                event_kind="impression",
                query=query,
                retrieval_mode=str(result["effective_mode"]),
                candidate_id=str(result["chunk_id"]),
                rank=rank,
                generation=str(result["generation"]),
                source_tool=source_tool,
            )
            if event is not None:
                events.append(event)
        if events:
            best_effort_record_events(events)
    except Exception:
        pass
    return results


def search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
    *,
    profile: str | None = None,
    catalog: GenerationCatalog | None = None,
    generation_embedder: object | None = None,
    generation_model_id: str | None = None,
    generation_model_revision: str | None = None,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Public search API — always routes through retrieval.retrieve()."""
    limit = _validate_search_limit(limit)
    if not query or not query.strip():
        return []
    from retrieval import retrieve_via_search_memory

    return retrieve_via_search_memory(
        query,
        scope=scope,
        limit=limit,
        force_rebuild=force_rebuild,
        project=project,
        since=since,
        as_of=as_of,
        semantic=semantic,
        page_paths=page_paths,
        graph=graph,
        rerank=rerank,
        source_tool=source_tool,
        emit_telemetry=emit_telemetry,
        profile=profile,
        catalog=catalog,
        generation_embedder=generation_embedder,
        generation_model_id=generation_model_id,
        generation_model_revision=generation_model_revision,
        deadline_monotonic=deadline_monotonic,
        max_candidates=max_candidates,
        cancelled=cancelled,
    )


def _legacy_fetch_rows(
    query: str,
    *,
    limit: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[tuple]:
    """One bounded BM25 read from the legacy index."""
    _check_legacy_stop(deadline, cancelled)
    connect_timeout = 5.0
    if deadline is not None:
        connect_timeout = max(0.0, min(connect_timeout, deadline - time.monotonic()))
    conn = sqlite3.connect(str(INDEX_FILE), timeout=connect_timeout)
    try:
        _check_legacy_stop(deadline, cancelled)
        with _legacy_sqlite_guard(conn, deadline, cancelled):
            return _legacy_bm25_rows(conn, query, limit)
    finally:
        conn.close()


def _legacy_rows_or_rebuild(
    query: str,
    pages: list[Path],
    *,
    limit: int,
    needs_rebuild: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[tuple] | None:
    """BM25 rows, rebuilding once if the index is missing or broken.

    None means the index could not be read at all, and the caller reads
    Markdown directly instead.
    """
    rebuilt = False
    if needs_rebuild:
        if not _rebuild_legacy_index(pages, deadline=deadline, cancelled=cancelled):
            return None
        rebuilt = True
    try:
        return _legacy_fetch_rows(query, limit=limit, deadline=deadline, cancelled=cancelled)
    except sqlite3.DatabaseError:
        pass
    if not rebuilt and not _rebuild_legacy_index(
        pages, deadline=deadline, cancelled=cancelled
    ):
        return None
    try:
        return _legacy_fetch_rows(query, limit=limit, deadline=deadline, cancelled=cancelled)
    except sqlite3.DatabaseError:
        return None


def _rebuild_legacy_index(
    pages: list[Path],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    try:
        if deadline is None and cancelled is None:
            _build_index(pages)
        else:
            _build_index(pages, deadline=deadline, cancelled=cancelled)
    except sqlite3.DatabaseError:
        return False
    return True


def _legacy_index_needs_rebuild(
    pages: list[Path],
    *,
    force_rebuild: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    if force_rebuild:
        return True
    if deadline is None and cancelled is None:
        return _needs_rebuild(pages)
    return _needs_rebuild(pages, deadline=deadline, cancelled=cancelled)


def _legacy_hit_rows(
    rows: list[tuple],
    *,
    query_lower: str,
    query_words: set[str],
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict]:
    hits: list[dict] = []
    for row in rows:
        _check_legacy_stop(deadline, cancelled)
        path, title, summary, proj, timestamp, rank = row
        if _legacy_row_excluded(str(path), str(timestamp or ""), since, as_of):
            continue
        score = _boosted_lexical_score(
            rank,
            path=path,
            title=title,
            row_project=proj or "",
            query_lower=query_lower,
            query_words=query_words,
            project=project,
        )
        hits.append(
            {
                "path": path,
                "title": title,
                "summary": summary[:120] if summary else "",
                "score": score,
                "bm25_score": score,
                "project": proj or "",
                "timestamp": timestamp or "",
                "candidate_id": Path(path).stem,
                "generation": "legacy",
            }
        )
    return hits


def _exact_page_hit(
    page: Path,
    *,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> dict | None:
    """A page whose filename is the query, read straight from Markdown.

    The index may not contain it yet; a filename match is too strong a signal
    to lose to a stale index.
    """
    try:
        relative_path = page.relative_to(ROOT).as_posix()
        raw = read_stable_bytes(page, MAX_PAGE_BYTES, label="exact filename search page")
        content = raw.decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    title, summary = _extract_title_and_summary(content, page.stem)
    page_project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
    timestamp = (_extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or "")[:10]
    if not _exact_page_eligible(
        relative_path,
        page_project=page_project,
        timestamp=timestamp,
        project=project,
        since=since,
        as_of=as_of,
    ):
        return None
    authority = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE) or ""
    return {
        "path": relative_path,
        "title": title,
        "summary": summary[:120],
        "score": round(10.0 * authority_weight(authority), 2),
        "bm25_score": 0.0,
        "project": page_project,
        "timestamp": timestamp,
        "candidate_id": page.stem,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "byte_start": 0,
        "byte_end": len(raw),
        "generation": "legacy",
        "authority": authority,
    }


def _exact_page_eligible(
    relative_path: str,
    *,
    page_project: str,
    timestamp: str,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> bool:
    if project and page_project.casefold() != project.casefold():
        return False
    if since and timestamp and timestamp < since[:10]:
        return False
    return not as_of or _valid_as_of(relative_path, as_of)


def _with_exact_page(
    hits: list[dict],
    pages: list[Path],
    normalized_stem: str,
    *,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict]:
    page = next(
        (item for item in pages if _normalized_filename_stem(item.name) == normalized_stem),
        None,
    )
    if page is None:
        return hits
    relative = page.relative_to(ROOT).as_posix()
    if any(hit["path"] == relative for hit in hits):
        return hits
    extra = _exact_page_hit(page, project=project, since=since, as_of=as_of)
    if extra is None:
        return hits
    return [*hits, extra]


def _promoted_filename_first(hits: list[dict], normalized_stem: str) -> list[dict]:
    """Keep a pure ranked list, but a filename match leads it."""
    matches = [
        hit for hit in hits if _normalized_filename_stem(str(hit["path"])) == normalized_stem
    ]
    if not matches:
        return hits
    matches.sort(
        key=lambda hit: (
            0 if "knowledge/notes/" in hit["path"] else 1,
            -hit["score"],
            hit["path"],
        )
    )
    best = matches[0]
    return [best, *(hit for hit in hits if hit["path"] != best["path"])]


def _legacy_lexical_hits(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    page_paths: list[Path] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Independent lexical backend used by retrieve() — no dense/graph fusion."""
    _check_legacy_stop(deadline, cancelled)
    if not query or not query.strip():
        return []
    pages = (
        page_paths
        if page_paths is not None
        else _collect_pages(scope, deadline=deadline or float("inf"))
    )
    if not pages:
        return []
    rows = _legacy_rows_or_rebuild(
        query,
        pages,
        limit=limit,
        needs_rebuild=_legacy_index_needs_rebuild(
            pages, force_rebuild=force_rebuild, deadline=deadline, cancelled=cancelled
        ),
        deadline=deadline,
        cancelled=cancelled,
    )
    if rows is None:
        return _direct_markdown_hits(
            query,
            pages,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            deadline=deadline,
            cancelled=cancelled,
        )
    query_lower = query.lower().strip()
    hits = _legacy_hit_rows(
        rows,
        query_lower=query_lower,
        query_words=set(query_lower.split()),
        project=project,
        since=since,
        as_of=as_of,
        deadline=deadline,
        cancelled=cancelled,
    )
    normalized_stem = _normalized_filename_stem(query)
    hits = _with_exact_page(
        hits, pages, normalized_stem, project=project, since=since, as_of=as_of
    )
    hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
    for hit in hits:
        hit["score"] = round(float(hit["score"]), 2)
        hit["bm25_score"] = round(float(hit["bm25_score"]), 2)
    hits = _promoted_filename_first(hits, normalized_stem)
    return hits[: max(limit * 3, limit)]


def _document_terms(page: Path, title: str, summary: str, body: str) -> set[str]:
    haystack = f"{page.stem.replace('-', ' ')} {title} {summary} {body}".casefold()
    return set(re.findall(r"\w+", haystack))


def _direct_match_score(
    page: Path, title: str, authority: str, query_terms: set[str]
) -> float:
    """Literal matching has no BM25, so the term count carries the base score."""
    score = float(len(query_terms))
    if query_terms.issubset(set(re.findall(r"\w+", title.casefold()))):
        score *= 3.0
    if query_terms.issubset(set(re.findall(r"\w+", page.stem.casefold()))):
        score *= 4.0
    return score * authority_weight(authority)


def _direct_page_hit(
    page: Path,
    *,
    query_terms: set[str],
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> dict | None:
    """One literal Markdown match, or None when the page does not qualify."""
    try:
        relative_path = page.relative_to(ROOT).as_posix()
        raw = read_stable_bytes(page, MAX_PAGE_BYTES, label="search page")
        content = raw.decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    title, summary = _extract_title_and_summary(content, page.stem)
    body = _strip_frontmatter(content)
    if not query_terms.issubset(_document_terms(page, title, summary, body)):
        return None
    page_project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
    timestamp = (_extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or "")[:10]
    if not _exact_page_eligible(
        relative_path,
        page_project=page_project,
        timestamp=timestamp,
        project=project,
        since=since,
        as_of=as_of,
    ):
        return None
    authority = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE) or ""
    score = round(_direct_match_score(page, title, authority, query_terms), 2)
    return {
        "path": relative_path,
        "title": title,
        "summary": summary[:120],
        "score": score,
        "bm25_score": score,
        "project": page_project,
        "timestamp": timestamp,
        "candidate_id": page.stem,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "byte_start": 0,
        "byte_end": len(raw),
        "generation": "legacy",
        "authority": authority,
        "fallback_reason": "legacy_sqlite_unavailable",
        "partial": True,
    }


def _direct_markdown_hits(
    query: str,
    pages: list[Path],
    *,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict]:
    """Return bounded literal matches from authoritative Markdown only."""
    query_terms = set(re.findall(r"\w+", query.casefold()))
    if not query_terms:
        raise LegacySearchUnavailable(
            "legacy SQLite unavailable and direct Markdown search found no matching page"
        )
    results: list[dict] = []
    for page in pages:
        _check_legacy_stop(deadline, cancelled)
        hit = _direct_page_hit(
            page,
            query_terms=query_terms,
            project=project,
            since=since,
            as_of=as_of,
        )
        if hit is not None:
            results.append(hit)
    results.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
    if not results:
        raise LegacySearchUnavailable(
            "legacy SQLite unavailable and direct Markdown search found no matching page"
        )
    return results[: max(limit * 3, limit)]


def _as_legacy_dense_row(item: Mapping[str, object], score_key: str) -> dict:
    row = dict(item)
    row["vector_score"] = row.get("vector_score", row.get(score_key))
    row.setdefault("candidate_id", Path(str(row.get("path") or "")).stem)
    row["generation"] = "legacy"
    return row


def _lance_dense_hits(
    query: str,
    pages: list[Path],
    *,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict] | None:
    """LanceDB results bound to this model and this source membership."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lance_store import have_lancedb
        from lance_store import vector_search as _lance_search

        if not have_lancedb():
            return None
        vectors = _embed_texts([query], is_query=True, deadline=deadline, cancelled=cancelled)
        if not vectors:
            return None
        results = _lance_search(
            vectors[0],
            limit * 3,
            project,
            since=since,
            as_of=as_of,
            expected_model_id=EMBEDDING_MODEL,
            expected_model_revision=EMBEDDING_MODEL_REVISION,
            expected_sources=_legacy_vector_source_membership(
                pages, deadline=deadline, cancelled=cancelled
            ),
        )
        _check_legacy_stop(deadline, cancelled)
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - an optional backend that cannot answer
        return None
    if not results:
        return None
    return [_as_legacy_dense_row(item, "score") for item in results]


def _numpy_dense_hits(
    query: str,
    pages: list[Path],
    *,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict] | None:
    try:
        results = _vector_search(
            query,
            pages,
            limit * 3,
            project,
            since,
            as_of,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - no usable vectors is not an error
        return None
    if results is None:
        return None
    return [_as_legacy_dense_row(row, "score") for row in results]


def _legacy_dense_hits(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    page_paths: list[Path] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict] | None:
    """Independent dense backend used by retrieve() — returns None if unavailable."""
    _check_legacy_stop(deadline, cancelled)
    if deadline is not None:
        return None
    if not query or not query.strip() or not _have_sentence_transformers():
        return None
    pages = (
        page_paths
        if page_paths is not None
        else _collect_pages(scope, deadline=deadline or float("inf"))
    )
    if not pages:
        return None
    hits = _lance_dense_hits(
        query,
        pages,
        limit=limit,
        project=project,
        since=since,
        as_of=as_of,
        deadline=deadline,
        cancelled=cancelled,
    )
    if hits is not None:
        return hits
    return _numpy_dense_hits(
        query,
        pages,
        limit=limit,
        project=project,
        since=since,
        as_of=as_of,
        deadline=deadline,
        cancelled=cancelled,
    )


def _generation_artifact_names(
    manifest: Mapping[str, object],
    *,
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
) -> tuple[str, ...]:
    """Vectors join the sealed set only when they are complete and wanted."""
    wanted_vectors = (
        semantic
        and embedder is not None
        and model_id is not None
        and model_revision is not None
        and manifest.get("vector_state") == "complete"
    )
    if wanted_vectors:
        return (GENERATION_FTS_ARTIFACT, *GENERATION_VECTOR_ARTIFACTS)
    return (GENERATION_FTS_ARTIFACT,)


def _active_manifest(
    catalog: GenerationCatalog, stop_options: Mapping[str, object]
) -> dict | None:
    try:
        manifest = catalog.get_active(**stop_options)
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _generation_vector_hits(
    query: str,
    catalog: GenerationCatalog,
    manifest: Mapping[str, object],
    connection: sqlite3.Connection,
    *,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    stop_options: Mapping[str, object],
) -> list[dict] | None:
    if embedder is None or model_id is None or model_revision is None:
        return None
    return _generation_vectors_search(
        query,
        catalog,
        manifest,
        connection,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
        scope=scope,
        limit=limit,
        project=project,
        since=since,
        as_of=as_of,
        **stop_options,
    )


def _mark_vectors_unavailable(
    lexical: list[dict], deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    for result in lexical:
        _check_generation_stop(deadline, cancelled)
        result["requested_mode"] = "hybrid"
        result["fallback_reason"] = "generation_vectors_unavailable"


def _generation_search_results(
    query: str,
    catalog: GenerationCatalog,
    manifest: Mapping[str, object],
    connection: sqlite3.Connection,
    *,
    artifact_names: tuple[str, ...],
    consumption_seal: object,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
    source_tool: str,
    emit_telemetry: bool,
    stop_options: Mapping[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict] | None:
    """Results from the sealed generation, or None when the seal no longer holds."""
    lexical = _generation_fts_search(
        query,
        manifest,
        connection,
        scope=scope,
        limit=limit,
        project=project,
        since=since,
        as_of=as_of,
        **stop_options,
    )

    def sealed() -> bool:
        return bool(
            _generation_consumption_unchanged(
                catalog, manifest, artifact_names, consumption_seal, **stop_options
            )
        )

    if semantic:
        vectors = _generation_vector_hits(
            query,
            catalog,
            manifest,
            connection,
            embedder=embedder,
            model_id=model_id,
            model_revision=model_revision,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            stop_options=stop_options,
        )
        if vectors is not None and sealed():
            return _finalize_generation_results(
                _fuse_generation_results(lexical, vectors, limit),
                query=query,
                source_tool=source_tool,
                emit_telemetry=emit_telemetry,
            )
        _mark_vectors_unavailable(lexical, deadline, cancelled)
    if not sealed():
        return None
    return _finalize_generation_results(
        lexical, query=query, source_tool=source_tool, emit_telemetry=emit_telemetry
    )


def _try_generation_search(
    query: str,
    catalog: GenerationCatalog,
    *,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
    source_tool: str,
    emit_telemetry: bool,
    stop_options: Mapping[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict] | None:
    """One attempt through the active generation; None means fall back."""
    manifest = _active_manifest(catalog, stop_options)
    if manifest is None:
        return None
    artifact_names = _generation_artifact_names(
        manifest,
        semantic=semantic,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
    )
    consumption_seal = _generation_consumption_seal(
        catalog, manifest, artifact_names, **stop_options
    )
    if consumption_seal is None:
        return None
    connection = _generation_connection(catalog, manifest, **stop_options)
    if connection is None:
        return None
    try:
        return _generation_search_results(
            query,
            catalog,
            manifest,
            connection,
            artifact_names=artifact_names,
            consumption_seal=consumption_seal,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            semantic=semantic,
            embedder=embedder,
            model_id=model_id,
            model_revision=model_revision,
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
            stop_options=stop_options,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except (
        OSError,
        PermissionError,
        sqlite3.Error,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None
    finally:
        connection.close()


def _search_backends(
    query: str,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
    *,
    catalog: GenerationCatalog | None = None,
    generation_embedder: object | None = None,
    generation_model_id: str | None = None,
    generation_model_revision: str | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Prefer a validated generation and otherwise preserve legacy search behavior."""
    limit = _validate_search_limit(limit)
    if not query or not query.strip():
        return []
    selected_catalog = catalog if catalog is not None else _active_generation_catalog()
    stop_options: dict[str, object] = {}
    if deadline is not None:
        stop_options["deadline"] = deadline
    if cancelled is not None:
        stop_options["cancelled"] = cancelled
    _check_generation_stop(deadline, cancelled)
    if selected_catalog is not None and not force_rebuild and page_paths is None:
        results = _try_generation_search(
            query,
            selected_catalog,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            semantic=semantic,
            embedder=generation_embedder,
            model_id=generation_model_id,
            model_revision=generation_model_revision,
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
            stop_options=stop_options,
            deadline=deadline,
            cancelled=cancelled,
        )
        if results is not None:
            return results
    _check_generation_stop(deadline, cancelled)
    return _legacy_search(
        query,
        scope,
        limit,
        force_rebuild,
        project,
        since,
        as_of,
        semantic,
        page_paths,
        graph,
        rerank,
        source_tool,
        emit_telemetry,
    )


def _legacy_bm25_rows(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[object, ...]]:
    """Adaptive fetch: short queries match more pages, so ask for more."""
    words = [word for word in query.split() if word]
    fts_query = " ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words)
    multiplier = 5 if len(words) <= 3 else 3
    return conn.execute(
        """
        SELECT path, title, summary, project, timestamp, bm25(pages) as rank
        FROM pages
        WHERE pages MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit * multiplier),
    ).fetchall()


def _legacy_row_excluded(path: str, timestamp: str, since: str | None, as_of: str | None) -> bool:
    if since and timestamp and timestamp[:10] < since:
        return True
    if not as_of:
        return False
    if timestamp and timestamp[:10] > as_of[:10]:
        return True
    return not _valid_as_of(path, as_of)


def _legacy_scored_rows(
    rows: list[tuple[object, ...]],
    *,
    query_lower: str,
    query_words: set[str],
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict]:
    scored = [
        _legacy_scored_row(
            row,
            query_lower=query_lower,
            query_words=query_words,
            project=project,
        )
        for row in rows
        if not _legacy_row_excluded(str(row[0]), str(row[4] or ""), since, as_of)
    ]
    # FTS5 returns bm25() order; the boosts above change it.
    scored.sort(key=lambda row: row["score"], reverse=True)
    return scored


def _legacy_scored_row(
    row: tuple[object, ...],
    *,
    query_lower: str,
    query_words: set[str],
    project: str | None,
) -> dict:
    path, title, summary, proj, timestamp, rank = row
    score = _boosted_lexical_score(
        rank,
        path=path,
        title=title,
        row_project=proj or "",
        query_lower=query_lower,
        query_words=query_words,
        project=project,
    )
    return {
        "path": path,
        "title": title,
        "summary": summary[:120] if summary else "",
        "score": round(score, 2),
        "project": proj or "",
        "timestamp": timestamp or "",
    }


def _exact_filename_answer(rows: list[dict], query: str, limit: int) -> list[dict] | None:
    """A page whose filename is the query wins outright, before any fusion.

    Otherwise a graph neighbour can promote a linked-but-wrong page above it.
    Duplicates prefer the canonical notes tree.
    """
    normalized = query.lower().strip().replace(" ", "-")
    matches = [row for row in rows[:10] if Path(row["path"]).stem.lower() == normalized]
    if not matches:
        return None
    matches.sort(key=lambda row: (0 if "knowledge/notes/" in row["path"] else 1, -row["score"]))
    best = matches[0]
    rest = [row for row in rows if row["path"] != best["path"]][: limit - 1]
    return [best, *rest]


def _legacy_vector_results(
    query: str,
    pages: list[Path],
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict] | None:
    """LanceDB when it is there, brute-force NumPy otherwise, None on failure."""
    results = _lance_vector_results(query, limit, project, since, as_of)
    if results is not None:
        return results
    try:
        return _vector_search(query, pages, limit * 3, project, since, as_of)
    except Exception as error:  # noqa: BLE001 - a failed optional signal is reported
        print(f"  (vector search failed: {error})", file=sys.stderr)
        return None


def _lance_vector_results(
    query: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict] | None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lance_store import have_lancedb
        from lance_store import vector_search as _lance_search

        if not have_lancedb():
            return None
        vectors = _embed_texts([query], is_query=True)
        if not vectors:
            return None
        return _lance_search(vectors[0], limit * 3, project, since=since, as_of=as_of) or None
    except Exception:  # noqa: BLE001 - optional backend
        return None


def _legacy_graph_boosts(
    graph: bool, bm25_results: list[dict], vector_results: list[dict] | None
) -> list[dict] | None:
    if not graph:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from graph_neighbors import boost_graph_neighbors

        return boost_graph_neighbors(bm25_results, vector_results)
    except Exception:  # noqa: BLE001 - optional signal
        return None


def _project_boosted_fusion(fused: list[dict], project: str | None) -> list[dict]:
    if not project:
        return fused
    for row in fused:
        if row.get("project", "").lower() == project.lower():
            row["fused_score"] = round(row["fused_score"] * 1.5, 4)
    fused.sort(key=lambda row: row.get("fused_score", 0), reverse=True)
    return fused


def _legacy_search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
) -> list[dict]:
    """Run a hybrid BM25 + optional vector search over the legacy index.

    `project` boosts pages tagged with that slug, `since` and `as_of` filter by
    time, and `semantic` adds vector search fused with BM25 through RRF so
    related pages surface when the keywords do not match.
    """
    if not query or not query.strip():
        return []
    pages = page_paths if page_paths is not None else _collect_pages(scope)
    if not pages:
        return []
    if force_rebuild or _needs_rebuild(pages):
        _build_index(pages)

    query_lower = query.lower().strip()
    conn = sqlite3.connect(str(INDEX_FILE))
    try:
        raw_rows = _legacy_bm25_rows(conn, query, limit)
    finally:
        conn.close()
    bm25_results = _legacy_scored_rows(
        raw_rows,
        query_lower=query_lower,
        query_words=set(query_lower.split()),
        project=project,
        since=since,
        as_of=as_of,
    )

    exact = _exact_filename_answer(bm25_results, query, limit)
    if exact is not None:
        return _finalize_results(
            query,
            exact,
            limit,
            retrieval_mode="exact",
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
        )

    vector_results = None
    if semantic and _have_sentence_transformers():
        vector_results = _legacy_vector_results(query, pages, limit, project, since, as_of)
    graph_boosts = _legacy_graph_boosts(graph, bm25_results, vector_results)

    if vector_results or graph_boosts:
        fused = _project_boosted_fusion(
            _rrf_fuse_triple(bm25_results, vector_results, graph_boosts), project
        )
        return _finalize_results(
            query,
            _maybe_rerank(query, fused, limit) if rerank else fused[:limit],
            limit,
            retrieval_mode="hybrid",
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
        )

    return _finalize_results(
        query,
        _maybe_rerank(query, bm25_results, limit) if rerank else bm25_results[:limit],
        limit,
        retrieval_mode="bm25",
        source_tool=source_tool,
        emit_telemetry=emit_telemetry,
    )


def _rrf_fuse_triple(
    bm25_results: list[dict],
    vector_results: list[dict] | None,
    graph_boosts: list[dict] | None,
    k: int = 60,
) -> list[dict]:
    """Triple-fusion RRF: BM25 + Vector + Graph-neighbor.

    Weighted RRF: BM25 gets weight 2 (most reliable for known-item
    retrieval), Vector gets weight 1 (helps with semantic queries),
    Graph gets weight 0.5 (soft boost through links).

    Standard unweighted RRF can HURT when BM25 is already correct:
    if BM25 has page at rank 1 but Vector has a different page at
    rank 1, the fusion pushes the correct page down. Weighting BM25
    higher prevents this regression.
    """
    scores: dict[str, float] = {}
    metadata: dict[str, dict] = {}

    # BM25 — weight 2.0 (most reliable signal)
    for rank, r in enumerate(bm25_results):
        path = r["path"]
        scores[path] = scores.get(path, 0) + 2.0 / (k + rank + 1)
        metadata[path] = r

    # Vector — weight 1.0 (helps when BM25 misses)
    if vector_results:
        for rank, r in enumerate(vector_results):
            path = r["path"]
            scores[path] = scores.get(path, 0) + 1.0 / (k + rank + 1)
            if path not in metadata:
                metadata[path] = r

    # Graph-neighbor — weight 0.5 (softest signal, boosts through links)
    if graph_boosts:
        for rank, r in enumerate(graph_boosts):
            path = r["path"]
            scores[path] = scores.get(path, 0) + 0.5 * r.get("graph_boost", 0) / (k * 2 + rank + 1)
            if path not in metadata:
                metadata[path] = {
                    "path": path,
                    "title": path.split("/")[-1].replace(".md", ""),
                    "summary": "",
                    "score": 0,
                    "project": "",
                    "timestamp": "",
                }

    sorted_paths = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for path, score in sorted_paths:
        r = metadata[path].copy()
        r["fused_score"] = round(score, 4)
        results.append(r)
    return results


_PAGE_STATUS_RE = re.compile(r"^status:\s*[\"\']?([^\"\'\n]+)[\"\']?\s*$", re.MULTILINE)


def _page_status(path: str) -> str:
    """The page's own status, or "active" when it does not say."""
    try:
        page = ROOT / path if not Path(path).is_absolute() else Path(path)
        content = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "active"
    status = _extract_frontmatter_field(content, _PAGE_STATUS_RE) or "active"
    return status.strip().lower()


def _legacy_vector_excluded(
    path: str, timestamp: str, *, since: str | None, as_of: str | None
) -> bool:
    if _legacy_row_excluded(path, timestamp, since, as_of):
        return True
    if as_of:
        return False
    # Without an as-of date, a superseded page is history.
    status = _page_status(path)
    return bool(status) and status != "active"


def _vector_cache_matrix(vectors_data: Mapping[str, object]) -> object | None:
    """The cached matrix as an ndarray, or None when the cache is unusable."""
    import numpy as np

    vectors = vectors_data["vectors"]
    if isinstance(vectors, list):
        # Refuse list-converted caches; require ndarray/mmap.
        return None
    matrix = np.asarray(vectors)
    if matrix.ndim != 2 or matrix.shape[0] != len(vectors_data["paths"]):
        return None
    return matrix


def _query_vector_for(
    query: str, matrix: object, *, deadline: float | None, cancelled
) -> object | None:
    import numpy as np

    embedded = _embed_texts([query], is_query=True, deadline=deadline, cancelled=cancelled)
    if not embedded:
        return None
    vector = np.asarray(embedded[0], dtype=np.float32)
    if vector.shape[0] != matrix.shape[1] or not np.isfinite(vector).all():
        return None
    return vector


def _vector_hit(
    index: int,
    similarity: float,
    vectors_data: Mapping[str, object],
    project: str | None,
) -> dict:
    path = vectors_data["paths"][index]
    row_project = vectors_data["projects"][index]
    score = round(float(similarity), 4)
    if project and row_project and row_project.lower() == project.lower():
        score = round(score * 1.5, 4)
    return {
        "path": path,
        "title": vectors_data["titles"][index],
        "summary": (vectors_data["summaries"][index] or "")[:120],
        "score": score,
        "project": row_project,
        "timestamp": vectors_data["timestamps"][index],
        "candidate_id": Path(path).stem,
    }


def _vector_search(
    query: str,
    pages: list[Path],
    limit: int,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict] | None:
    """Rank pages by cosine similarity against the cached page embeddings."""
    _check_legacy_stop(deadline, cancelled)
    vectors_data = _load_or_build_vectors(pages, deadline=deadline, cancelled=cancelled)
    if not vectors_data:
        return None
    matrix = _vector_cache_matrix(vectors_data)
    if matrix is None:
        return None
    query_vector = _query_vector_for(
        query, matrix, deadline=deadline, cancelled=cancelled
    )
    if query_vector is None:
        return None
    similarities = _cosine_similarity(query_vector, matrix)
    results = []
    for index, similarity in enumerate(similarities):
        _check_legacy_stop(deadline, cancelled)
        if _legacy_vector_excluded(
            vectors_data["paths"][index],
            str(vectors_data["timestamps"][index] or ""),
            since=since,
            as_of=as_of,
        ):
            continue
        results.append(_vector_hit(index, similarity, vectors_data, project))
    results.sort(key=lambda hit: (-hit["score"], hit["path"]))
    return results[:limit]


def _legacy_vector_documents(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, list[str]]:
    documents = {
        "source_paths": [],
        "source_sha256": [],
        "titles": [],
        "summaries": [],
        "projects": [],
        "timestamps": [],
        "texts": [],
    }
    ordered = sorted(
        (page for page in pages if page.exists()),
        key=lambda page: page.relative_to(ROOT).as_posix(),
    )
    for page in ordered:
        _check_legacy_stop(deadline, cancelled)
        raw = read_stable_bytes(page, MAX_PAGE_BYTES, label="legacy vector source")
        content = raw.decode("utf-8", errors="ignore")
        title, summary = _extract_title_and_summary(content, page.stem)
        body = _strip_frontmatter(content)[:500]
        project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
        timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
        documents["source_paths"].append(page.relative_to(ROOT).as_posix())
        documents["source_sha256"].append(hashlib.sha256(raw).hexdigest())
        documents["titles"].append(title)
        documents["summaries"].append(summary)
        documents["projects"].append(project.lower())
        documents["timestamps"].append(timestamp[:10] if timestamp else "")
        documents["texts"].append(f"{title}. {summary}. {body[:300]}")
    return documents


def _legacy_vector_source_membership(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[tuple[str, str]]:
    live = _legacy_vector_documents(pages, deadline=deadline, cancelled=cancelled)
    return list(zip(live["source_paths"], live["source_sha256"], strict=True))


def _load_or_build_vectors(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict | None:
    """Load cached embeddings or build them fresh.

    v4.0: Uses memory-mapped .npy format for vectors (instant load) +
    small .json for metadata. Existing artifacts are accepted only when every
    metadata value still matches the configured model and live source bytes.
    """
    # Try .npy format (fast, memory-mapped). Never convert mmap → Python list.
    _check_legacy_stop(deadline, cancelled)
    if VECTOR_NPY.exists() and VECTOR_META.exists():
        try:
            import numpy as np

            cache_meta = json.loads(VECTOR_META.read_text(encoding="utf-8"))
            vectors = np.load(str(VECTOR_NPY), mmap_mode="r", allow_pickle=False)
            live = _legacy_vector_documents(
                pages, deadline=deadline, cancelled=cancelled
            )
            finite = bool(np.isfinite(vectors).all())
            expected = {
                "schema_version": "legacy-vectors/v1",
                "model_id": EMBEDDING_MODEL,
                "model_revision": EMBEDDING_MODEL_REVISION,
                "dimensions": EMBEDDING_DIM,
                "source_paths": live["source_paths"],
                "source_sha256": live["source_sha256"],
                "titles": live["titles"],
                "summaries": live["summaries"],
                "projects": live["projects"],
                "timestamps": live["timestamps"],
                "dtype": str(vectors.dtype),
                "shape": list(vectors.shape),
                "finite": finite,
                "artifact_sha256": _artifact_descriptor(VECTOR_NPY, "vectors.npy")[
                    "sha256"
                ],
            }
            if (
                cache_meta != expected
                or vectors.ndim != 2
                or vectors.shape != (len(live["source_paths"]), EMBEDDING_DIM)
                or vectors.dtype != np.dtype(np.float32)
                or not finite
            ):
                return None
            return {**live, **cache_meta, "paths": live["source_paths"], "vectors": vectors}
        except TimeoutError:
            raise
        except Exception:
            return None

    return _build_vectors(pages, deadline=deadline, cancelled=cancelled)


def _build_vectors(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict | None:
    """Build embeddings for all pages. Returns None if model unavailable."""
    _check_legacy_stop(deadline, cancelled)
    embedder = _get_embedder()
    if not embedder:
        return None

    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    try:
        live = _legacy_vector_documents(
            pages, deadline=deadline, cancelled=cancelled
        )
    except (OSError, ValueError):
        return None
    texts_list = live.pop("texts")

    if not texts_list:
        return None

    # Embed all texts
    try:
        import numpy as np

        vectors = np.asarray(
            embedder.encode(texts_list, show_progress_bar=False, convert_to_numpy=True)
        )
        _check_legacy_stop(deadline, cancelled)
    except TimeoutError:
        raise
    except Exception:
        return None
    if (
        vectors.ndim != 2
        or vectors.shape != (len(texts_list), EMBEDDING_DIM)
        or vectors.dtype.kind not in "fiu"
        or not np.isfinite(vectors).all()
    ):
        return None
    vectors = np.ascontiguousarray(vectors, dtype=np.float32)

    # v4.0: Save vectors as binary .npy (memory-mapped, fast load).
    # Save metadata as small JSON (no vectors → small file).
    try:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(str(VECTOR_NPY), vectors, allow_pickle=False)
        metadata = {
            "schema_version": "legacy-vectors/v1",
            "model_id": EMBEDDING_MODEL,
            "model_revision": EMBEDDING_MODEL_REVISION,
            "dimensions": EMBEDDING_DIM,
            **live,
            "dtype": str(vectors.dtype),
            "shape": list(vectors.shape),
            "finite": bool(np.isfinite(vectors).all()),
            "artifact_sha256": _artifact_descriptor(VECTOR_NPY, "vectors.npy")[
                "sha256"
            ],
        }
        atomic_write(
            VECTOR_META,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    except TimeoutError:
        raise
    except Exception:
        return None

    return {**live, **metadata, "paths": live["source_paths"], "vectors": vectors}


def main() -> int:
    p = argparse.ArgumentParser(description="Built-in FTS5 search over the vault.")
    p.add_argument("query", nargs="?", default=None, help="Search query")
    p.add_argument("--scope", choices=["all", "wiki", "memory", "knowledge"], default="all")
    p.add_argument("--limit", type=_cli_search_limit, default=10)
    p.add_argument("--project", default=None, help="Boost results from this project slug")
    p.add_argument("--since", default=None, help="Only results since YYYY-MM-DD")
    p.add_argument("--as-of", dest="as_of", default=None, help="Only results valid on YYYY-MM-DD")
    p.add_argument("--semantic", action="store_true", help="Enable vector search (needs sentence-transformers)")
    p.add_argument(
        "--profile",
        choices=[
            "DIRECT",
            "EXACT",
            "BASE",
            "HYBRID",
            "GRAPH",
            "TEMPORAL",
            "REPO_MAP",
            "IMPACT",
            "GLOBAL",
            "CACHED_FULL",
        ],
        default=None,
        help="Requested retrieval profile (Task 11 planner)",
    )
    p.add_argument("--no-graph", action="store_true", help="Disable graph-neighbor signal")
    p.add_argument("--no-rerank", action="store_true", help="Disable cross-encoder reranker")
    p.add_argument("--rebuild", action="store_true", help="Force index rebuild")
    p.add_argument("--status", action="store_true", help="Show index stats")
    p.add_argument("--stdin", action="store_true", help="Read query from stdin (injection-safe)")
    args = p.parse_args()

    if args.stdin:
        args.query = sys.stdin.read().strip()

    if args.status:
        pages = _collect_pages("all")
        if INDEX_FILE.exists():
            conn = sqlite3.connect(str(INDEX_FILE))
            count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            conn.close()
            print(f"Index: {INDEX_FILE}")
            print(f"  Pages indexed: {count}")
            print(f"  Pages on disk: {len(pages)}")
            print(f"  Index size: {INDEX_FILE.stat().st_size} bytes")
            print(f"  Needs rebuild: {_needs_rebuild(pages)}")
        else:
            print(f"Index: not built ({len(pages)} pages would be indexed)")
        return 0

    if args.rebuild:
        pages = _collect_pages(args.scope)
        print(f"Rebuilding index with {len(pages)} pages...")
        t0 = time.time()
        _build_index(pages)
        print(f"Done in {time.time() - t0:.2f}s")
        return 0

    if not args.query:
        print("Usage: python search_memory.py \"<query>\"", file=sys.stderr)
        return 1

    t0 = time.time()
    try:
        results = search(
            args.query, args.scope, args.limit,
            force_rebuild=args.rebuild,
            project=args.project,
            since=args.since,
            as_of=args.as_of,
            semantic=args.semantic,
            profile=args.profile,
            graph=not args.no_graph,
            rerank=not args.no_rerank,
        )
    except LegacySearchUnavailable as error:
        print(f"search_memory: {error}", file=sys.stderr)
        return 2
    elapsed = time.time() - t0

    if not results:
        print(f"No results for '{args.query}' ({elapsed:.3f}s)")
        return 0

    print(f"Found {len(results)} result(s) for '{args.query}' ({elapsed:.3f}s):\n")
    for i, r in enumerate(results, 1):
        proj_tag = f" [{r['project']}]" if r["project"] else ""
        ts_tag = f" ({r['timestamp']})" if r["timestamp"] else ""
        print(f"{i}. [{r['score']}] {r['title']}{proj_tag}{ts_tag}")
        print(f"   {r['path']}")
        if r["summary"]:
            print(f"   {r['summary']}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
