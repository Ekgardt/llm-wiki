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
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, nullcontext
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
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.executescript(
            """
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
        )
        metadata = {
            "schema_version": GENERATION_SEARCH_SCHEMA_VERSION,
            "collector_version": snapshot.collector_version,
            "extractor_version": snapshot.extractor_version,
            "tokenizer_version": GENERATION_TOKENIZER_VERSION,
            "tokenizer_config_sha256": GENERATION_TOKENIZER_CONFIG_SHA256,
            "source_manifest_sha256": snapshot.corpus_sha256,
            "chunk_count": str(len(snapshot.chunks)),
        }
        database.executemany(
            "INSERT INTO generation_metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        def rows():
            for order, chunk in enumerate(snapshot.chunks):
                _check_generation_stop(deadline, cancelled)
                title = chunk.heading_ancestry[-1] if chunk.heading_ancestry else Path(
                    chunk.source_path
                ).stem
                yield (
                    chunk.id,
                    order,
                    chunk.source_id,
                    chunk.source_path,
                    chunk.source_sha256,
                    chunk.parent_page,
                    json.dumps(
                        chunk.heading_ancestry,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
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
        database.executemany(
            "INSERT INTO chunks VALUES ("
            + ",".join("?" for _ in range(22))
            + ")",
            rows(),
        )
        database.commit()
        if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("generation FTS integrity check failed")
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
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _call_generation_embedder(embedder: object, texts: list[str]):
    if callable(embedder):
        return embedder(texts)
    encode = getattr(embedder, "encode", None)
    if not callable(encode):
        raise TypeError("embedder must be callable or provide encode()")
    return encode(texts, show_progress_bar=False, convert_to_numpy=True)


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
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if not model_id or not model_revision:
        raise ValueError("model ID and revision must be non-empty")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("dimensions must be a positive integer")
    directory = _generation_directory(generation_directory)
    destinations = [directory / name for name in GENERATION_VECTOR_ARTIFACTS]
    for destination in destinations:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)

    import numpy as np

    texts = [chunk.text for chunk in snapshot.chunks]
    matrix = np.asarray(_call_generation_embedder(embedder, texts))
    if matrix.ndim != 2 or matrix.shape != (len(snapshot.chunks), dimensions):
        raise ValueError("embedder returned a matrix with incompatible shape")
    if matrix.dtype.kind not in "fiu" or not np.isfinite(matrix).all():
        raise ValueError("embedder returned a non-finite numeric matrix")
    matrix = np.ascontiguousarray(matrix, dtype=np.float32)
    metadata = {
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
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for path in (temporary_json, temporary_npy):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
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
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")

    def remaining() -> float | None:
        _check_generation_stop(deadline, cancelled)
        if deadline is None:
            return None
        value = float(deadline) - time.monotonic()
        if value <= 0:
            raise TimeoutError("generation publication deadline reached")
        return value

    wait_seconds = remaining()
    if coordinator is None:
        gate = nullcontext()
    elif wait_seconds is None:
        gate = coordinator.writer_gate()
    else:
        gate = coordinator.writer_gate(wait_seconds=wait_seconds)
    with gate:
        registered = False
        try:
            _check_generation_stop(deadline, cancelled)
            if expected_repository_scope is not None:
                from repository_scope import RepositoryScope, resolve_repository_scope

                if not isinstance(expected_repository_scope, RepositoryScope):
                    raise TypeError("expected_repository_scope must be a RepositoryScope")
                live_scope = resolve_repository_scope(
                    vault, deadline=deadline, cancelled=cancelled
                )
                if live_scope != expected_repository_scope:
                    raise ValueError("publication root does not match generation repository scope")
            validation_options = {"coordinator": None}
            if cancelled is not None:
                validation_options["cancelled"] = cancelled
            if deadline is not None:
                validation_options["deadline_seconds"] = remaining()
            validate_live_snapshot(snapshot, vault, **validation_options)
            remaining()
            catalog_options = {}
            if deadline is not None:
                catalog_options["deadline"] = float(deadline)
            if cancelled is not None:
                catalog_options["cancelled"] = cancelled
            if candidate is None:
                catalog.register(generation_id, **catalog_options)
            else:
                catalog._register_validated(candidate, **catalog_options)  # noqa: SLF001
            registered = True
            remaining()
            if candidate is None:
                activated = catalog.activate(
                    generation_id,
                    expected_active=expected_active,
                    **catalog_options,
                )
            else:
                activated = catalog._activate_validated(  # noqa: SLF001
                    candidate,
                    expected_active=expected_active,
                    **catalog_options,
                )
            if not activated:
                discard = getattr(catalog, "discard_unactivated", None)
                if callable(discard):
                    discard(generation_id, **catalog_options)
            return activated
        except BaseException:
            if registered:
                discard = getattr(catalog, "discard_unactivated", None)
                if callable(discard):
                    discard(generation_id, **catalog_options)
            raise


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


def _collect_pages(
    scope: str = "all",
    *,
    knowledge_dir: Path | None = None,
    root: Path | None = None,
    deadline: float = float("inf"),
) -> list[Path]:
    """Collect bounded regular markdown pages without following links."""
    pages: list[Path] = []
    seen: set[Path] = set()
    inspected_entries = 0
    traversed_directories = 0
    selected_knowledge = knowledge_dir or KNOWLEDGE_DIR
    source_root = root or selected_knowledge.parents[1]

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("searchable page collection deadline reached")

    def is_safe(path: Path, *, directory: bool) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        unsafe = path.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        return not unsafe and expected

    roots: list[Path] = []
    # All scope values resolve to the single knowledge/notes tree after the
    # three-zone consolidation; "wiki" and "memory" are kept as legacy aliases.
    if scope in ("wiki", "memory", "knowledge", "all"):
        roots.append(selected_knowledge)

    for root in roots:
        check_deadline()
        if not root.exists():
            continue
        try:
            relative_root = root.relative_to(source_root)
        except ValueError as exc:
            raise OSError("searchable knowledge root escapes the vault") from exc
        components = [source_root]
        component = source_root
        for part in relative_root.parts:
            component /= part
            components.append(component)
        if not all(is_safe(component, directory=True) for component in components):
            raise OSError("unsafe knowledge directory")
        pending = [(root, 0)]
        while pending:
            current_path, depth = pending.pop()
            check_deadline()
            traversed_directories += 1
            if traversed_directories > MAX_SEARCH_DIRECTORIES:
                raise ValueError("searchable directory limit exceeded")
            if depth > MAX_SEARCH_DEPTH:
                raise ValueError("searchable directory depth limit exceeded")
            safe_directories: list[Path] = []
            filenames: list[str] = []
            try:
                with os.scandir(current_path) as entries:
                    for entry in entries:
                        check_deadline()
                        inspected_entries += 1
                        if inspected_entries > MAX_SEARCH_ENTRIES:
                            raise ValueError("searchable entry limit exceeded")
                        name = entry.name
                        path = current_path / name
                        if name not in SKIP_DIRS and is_safe(path, directory=True):
                            safe_directories.append(path)
                        elif name.endswith(".md"):
                            filenames.append(name)
            except OSError:
                continue
            if depth >= MAX_SEARCH_DEPTH and safe_directories:
                raise ValueError("searchable directory depth limit exceeded")
            check_deadline()
            for name in sorted(filenames):
                check_deadline()
                md = current_path / name
                if not name.endswith(".md") or name in SKIP_NAMES or md in seen:
                    continue
                if not is_safe(md, directory=False):
                    continue
                try:
                    content = read_stable_bytes(md, MAX_PAGE_BYTES, label="search page").decode(
                        "utf-8", errors="ignore"
                    )
                except (OSError, ValueError):
                    continue
                fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                if fm:
                    status_m = re.search(r"^status:\s*(.+?)\s*$", fm.group(1), re.MULTILINE)
                    if status_m and status_m.group(1).strip() in ("superseded", "archived"):
                        continue
                seen.add(md)
                pages.append(md)
                if len(pages) > MAX_SEARCHABLE_PAGES:
                    raise ValueError("searchable page limit exceeded")
            check_deadline()
            for directory in reversed(sorted(safe_directories)):
                pending.append((directory, depth + 1))
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

# Higher weight = preferred in ranking (typed provenance).
AUTHORITY_WEIGHTS = {
    "user": 1.35,
    "human": 1.35,
    "web": 1.1,
    "ai-derived": 1.0,
    "ai": 1.0,
    "inferred": 0.8,
    "unknown": 1.0,
}


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


def _needs_rebuild(
    pages: list[Path],
    *,
    root: Path | None = None,
    index_file: Path | None = None,
    index_manifest: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Check if any page is newer than the index, or if pages were added/removed."""
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
        connect_options = {}
        if deadline is not None:
            connect_options["timeout"] = max(0.0, min(5.0, deadline - time.monotonic()))
        with closing(sqlite3.connect(str(current_index), **connect_options)) as conn:
            with _legacy_sqlite_guard(conn, deadline, cancelled):
                columns = tuple(
                    row[1] for row in conn.execute("PRAGMA table_info(pages)")
                )
                schema_row = conn.execute(
                    "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                    ("pages",),
                ).fetchone()
                manifest_row = conn.execute(
                    "SELECT value FROM index_metadata WHERE key = 'paths'"
                ).fetchone()
        schema_sql = schema_row[0] if schema_row and isinstance(schema_row[0], str) else ""
        normalized_schema = " ".join(schema_sql.casefold().split())
        if columns != SEARCH_INDEX_COLUMNS or re.search(
            r"\bCREATE\s+VIRTUAL\s+TABLE\b.*\bUSING\s+fts5\s*\(",
            schema_sql,
            re.IGNORECASE | re.DOTALL,
        ) is None or any(
            marker not in normalized_schema
            for marker in (
                "path unindexed",
                "project unindexed",
                "timestamp unindexed",
                "tokenize = 'porter unicode61'",
            )
        ):
            return True
        if manifest_row is None:
            return True
        manifest_paths = json.loads(manifest_row[0])
    except sqlite3.Error:
        return True
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    # The manifest is stored inside the SQLite candidate, so index and source
    # membership become active through the same atomic file replacement.
    _check_legacy_stop(deadline, cancelled)
    current_paths = sorted(p.relative_to(source_root).as_posix() for p in pages)
    if manifest_paths != current_paths:
        return True
    _check_legacy_stop(deadline, cancelled)
    index_mtime = current_index.stat().st_mtime
    for p in pages:
        _check_legacy_stop(deadline, cancelled)
        try:
            if p.stat().st_mtime > index_mtime:
                return True
        except OSError:
            continue
    return False


def _is_transient_windows_access_error(error: OSError) -> bool:
    return sys.platform == "win32" and isinstance(error, PermissionError) and (
        getattr(error, "winerror", None) in {5, 32, 33}
        or error.errno == 13
    )


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

    while True:
        _check_legacy_stop(end, cancelled)
        try:
            descriptor = os.open(
                str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            pass
        except PermissionError as error:
            if not _is_transient_windows_access_error(error):
                raise
        else:
            try:
                os.write(descriptor, payload)
            except BaseException:
                os.close(descriptor)
                try:
                    lock_file.unlink()
                except OSError:
                    pass
                raise
            else:
                os.close(descriptor)
            break

        try:
            age = time.time() - lock_file.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= _INDEX_SWAP_STALE_SECONDS:
            try:
                owner = json.loads(lock_file.read_text(encoding="utf-8"))
                owner_pid = owner.get("pid")
                owner_token = owner.get("token")
                valid_owner = (
                    isinstance(owner_pid, int)
                    and isinstance(owner_token, str)
                    and bool(owner_token)
                )
            except (OSError, json.JSONDecodeError, AttributeError):
                valid_owner = False
                owner_pid = 0
            if not valid_owner or not _is_pid_alive(owner_pid):
                try:
                    lock_file.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
        _check_legacy_stop(end, cancelled)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Could not acquire index swap lock: {lock_file}")
        time.sleep(min(poll, remaining))
        _check_legacy_stop(end, cancelled)

    try:
        yield
    finally:
        try:
            owner = json.loads(lock_file.read_text(encoding="utf-8"))
            if owner.get("token") == token:
                lock_file.unlink()
        except (OSError, json.JSONDecodeError, AttributeError):
            pass


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


def _build_index(
    pages: list[Path],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Build the FTS5 index from scratch (atomically).

    Builds into a temporary database file, then atomically replaces the
    live index via ``os.replace``. This ensures concurrent searches never
    see a partially-built index or a missing-index window.
    """
    _check_legacy_stop(deadline, cancelled)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{INDEX_FILE.name}.", suffix=".tmp", dir=INDEX_DIR
    )
    os.close(fd)
    tmp_file = Path(tmp_name)
    conn = None
    source_digests: dict[Path, str] = {}
    manifest_paths = sorted(p.relative_to(ROOT).as_posix() for p in pages)
    try:
        conn = sqlite3.connect(str(tmp_file))
        conn.execute(
            """
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
        )
        conn.execute(
            "CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) "
            "WITHOUT ROWID"
        )
        conn.execute(
            "INSERT INTO index_metadata (key, value) VALUES ('paths', ?)",
            (json.dumps(manifest_paths, separators=(",", ":")),),
        )

        for p in pages:
            _check_legacy_stop(deadline, cancelled)
            try:
                source = read_stable_bytes(p, MAX_PAGE_BYTES, label="search page")
            except (OSError, ValueError):
                continue
            source_digests[p] = hashlib.sha256(source).hexdigest()
            content = source.decode("utf-8", errors="ignore")
            title, summary = _extract_title_and_summary(content, p.stem)
            body = _strip_frontmatter(content)
            rel_path = p.relative_to(ROOT).as_posix()
            project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
            timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
            # Truncate timestamp to date only for filtering
            timestamp = timestamp[:10] if timestamp else ""
            slug = p.stem.replace("-", " ").replace("_", " ")
            conn.execute(
                "INSERT INTO pages (path, title, summary, body, project, timestamp, slug) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rel_path, title, summary, body, project.lower(), timestamp, slug),
            )

        conn.commit()
        _check_legacy_stop(deadline, cancelled)
        conn.close()
        conn = None

        # Builders do the expensive work independently, then briefly serialize
        # the atomic live-index swap. Windows readers may transiently deny it.
        with _index_swap_lock(deadline=deadline, cancelled=cancelled):
            for path, expected_digest in source_digests.items():
                _check_legacy_stop(deadline, cancelled)
                try:
                    current = read_stable_bytes(
                        path, MAX_PAGE_BYTES, label="search page publication"
                    )
                except (OSError, ValueError):
                    return
                if hashlib.sha256(current).hexdigest() != expected_digest:
                    return
            _replace_live_index(
                tmp_file, deadline=deadline, cancelled=cancelled
            )

            # Keep the manifest paired with the winning index build.
            try:
                atomic_write(
                    INDEX_MANIFEST,
                    json.dumps(manifest_paths),
                )
            except OSError:
                pass  # best-effort
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
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
    auth = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE)
    if not auth:
        return 1.0
    return AUTHORITY_WEIGHTS.get(auth.strip().lower(), 1.0)


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


def _generation_authoritative_sources(
    generation_path: Path,
    manifest: dict[str, object],
    *,
    state_root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, object]]:
    _check_generation_stop(deadline, cancelled)
    source_manifest_path = generation_path / "source-manifest.json"
    expected_manifest = validate_runtime_file(
        source_manifest_path, state_root, max_bytes=MAX_CORPUS_TOTAL_BYTES
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source_manifest_path, flags)
    try:
        opened_manifest = os.fstat(descriptor)
        if not os.path.samestat(expected_manifest, opened_manifest):
            raise PermissionError("generation source manifest identity changed before read")
        chunks = []
        total = 0
        while True:
            _check_generation_stop(deadline, cancelled)
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CORPUS_TOTAL_BYTES:
                raise ValueError("generation source manifest exceeds its byte ceiling")
            chunks.append(chunk)
        after_manifest = os.fstat(descriptor)
        if not os.path.samestat(opened_manifest, after_manifest) or (
            opened_manifest.st_size,
            opened_manifest.st_mtime_ns,
        ) != (after_manifest.st_size, after_manifest.st_mtime_ns):
            raise PermissionError("generation source manifest changed during read")
        source_manifest_raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    current_manifest = source_manifest_path.stat(follow_symlinks=False)
    if not os.path.samestat(after_manifest, current_manifest):
        raise PermissionError("generation source manifest identity changed after read")
    _check_generation_stop(deadline, cancelled)
    try:
        source_manifest_value = json.loads(source_manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("generation source manifest is invalid") from exc
    source_manifest = validate_canonical_source_manifest(source_manifest_value)
    if (
        canonical_json_bytes(source_manifest) != source_manifest_raw
        or hashlib.sha256(source_manifest_raw).hexdigest()
        != manifest.get("source_manifest_sha256")
    ):
        raise ValueError("generation source manifest hash is invalid")

    evidence_path = generation_path / "evidence.sqlite3"
    validate_runtime_file(evidence_path, state_root, max_bytes=16 * 1024 * 1024 * 1024)
    uri = f"{evidence_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    source_metadata: list[tuple[str, str, str, int]] = []
    seen_source_ids: set[str] = set()
    total_bytes = 0
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
        with _generation_sqlite_guard(database, deadline, cancelled):
            rows = database.execute(
                "SELECT source_id, relative_path, sha256, size, length(content) FROM source "
                "ORDER BY relative_path, source_id LIMIT ?",
                (MAX_CORPUS_FILES + 1,),
            )
            for row in rows:
                _check_generation_stop(deadline, cancelled)
                if len(source_metadata) >= MAX_CORPUS_FILES:
                    raise ValueError("generation source row ceiling exceeded")
                source_id, relative_path, digest, size, content_size = row
                if (
                    not isinstance(source_id, str)
                    or source_id in seen_source_ids
                    or not isinstance(relative_path, str)
                    or not isinstance(digest, str)
                    or not isinstance(size, int)
                    or not isinstance(content_size, int)
                    or content_size > MAX_CORPUS_FILE_BYTES
                    or size != content_size
                ):
                    raise ValueError("generation evidence source rows are invalid")
                total_bytes += content_size
                if total_bytes > MAX_CORPUS_TOTAL_BYTES:
                    raise ValueError("generation evidence source bytes exceed their ceiling")
                seen_source_ids.add(source_id)
                source_metadata.append((source_id, relative_path, digest, size))
            sources: dict[str, dict[str, object]] = {}
            for source_id, relative_path, digest, size in source_metadata:
                _check_generation_stop(deadline, cancelled)
                content_row = database.execute(
                    "SELECT content FROM source WHERE source_id = ?", (source_id,)
                ).fetchone()
                if content_row is None:
                    raise ValueError("generation evidence source content is missing")
                content = bytes(content_row[0])
                if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("generation evidence source content is invalid")
                sources[source_id] = {
                    "relative_path": relative_path,
                    "sha256": digest,
                    "content": content,
                }
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


def _valid_generation_fts_contents(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        return False
    metadata_schema = tuple(
        (row[1], row[2].upper(), row[3], row[5])
        for row in connection.execute("PRAGMA table_info(generation_metadata)")
    )
    chunk_columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(chunks)"))
    if metadata_schema != (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ) or chunk_columns != GENERATION_FTS_COLUMNS:
        return False
    schema_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='chunks'"
    ).fetchone()
    if not schema_row or not isinstance(schema_row[0], str):
        return False
    normalized_schema = " ".join(schema_row[0].casefold().split())
    prefix = "create virtual table chunks using fts5("
    if not normalized_schema.startswith(prefix) or not normalized_schema.endswith(")"):
        return False
    arguments = [
        argument.strip()
        for argument in normalized_schema[len(prefix) : -1].split(",")
    ]
    expected_arguments = [
        *(f"{column} unindexed" for column in GENERATION_FTS_COLUMNS[:-2]),
        "title",
        "content",
        "tokenize = 'porter unicode61'",
    ]
    if arguments != expected_arguments:
        return False
    metadata_rows = list(
        connection.execute(
            "SELECT key, value FROM generation_metadata LIMIT ?",
            (len(GENERATION_METADATA_KEYS) + 1,),
        )
    )
    if (
        len(metadata_rows) != len(GENERATION_METADATA_KEYS)
        or {row[0] for row in metadata_rows} != GENERATION_METADATA_KEYS
        or any(not isinstance(row[1], str) for row in metadata_rows)
    ):
        return False
    metadata = dict(metadata_rows)
    expected = {
        "schema_version": GENERATION_SEARCH_SCHEMA_VERSION,
        "collector_version": manifest.get("collector_version"),
        "extractor_version": manifest.get("extractor_version"),
        "tokenizer_version": GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": GENERATION_TOKENIZER_CONFIG_SHA256,
        "source_manifest_sha256": manifest.get("source_manifest_sha256"),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        return False
    expected_chunks: list[tuple[object, ...]] = []
    if authoritative_sources is not None:
        ordered_sources = sorted(
            authoritative_sources.items(),
            key=lambda item: (str(item[1]["relative_path"]), item[0]),
        )
        for source_id, source in ordered_sources:
            _check_generation_stop(deadline, cancelled)
            source_path = str(source["relative_path"])
            source_sha256 = str(source["sha256"])
            source_content = source["content"]
            if not isinstance(source_content, bytes):
                return False
            for chunk in canonical_retrieval_chunks(
                source_id=source_id,
                source_path=source_path,
                source_sha256=source_sha256,
                content=source_content,
                extractor_version=str(manifest.get("extractor_version")),
                deadline=deadline,
                cancelled=cancelled,
            ):
                title = chunk.heading_ancestry[-1] if chunk.heading_ancestry else Path(
                    chunk.source_path
                ).stem
                expected_chunks.append(
                    (
                        chunk.id,
                        len(expected_chunks),
                        chunk.source_id,
                        chunk.source_path,
                        chunk.source_sha256,
                        chunk.parent_page,
                        json.dumps(
                            chunk.heading_ancestry,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
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
                )
                if len(expected_chunks) > MAX_GENERATION_FTS_CHUNKS:
                    return False
    counted_rows = list(
        connection.execute(
            "SELECT 1 FROM chunks LIMIT ?", (MAX_GENERATION_FTS_CHUNKS + 1,)
        )
    )
    count = len(counted_rows)
    if (
        count > MAX_GENERATION_FTS_CHUNKS
        or metadata.get("chunk_count") != str(count)
        or (
            authoritative_sources is not None
            and count != len(expected_chunks)
        )
    ):
        return False
    seen_chunk_ids: set[str] = set()
    rows = connection.execute(
        "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
        "parent_page, heading_ancestry, byte_start, byte_end, line_start, line_end, "
        "span_sha256, type, project, authority, confidence, status, valid_from, "
        "valid_to, language, title, content FROM chunks ORDER BY chunk_order"
    )
    for expected_order, row in enumerate(rows):
        _check_generation_stop(deadline, cancelled)
        (
            chunk_id,
            chunk_order,
            source_id,
            source_path,
            source_sha256,
            parent_page,
            ancestry_json,
            byte_start,
            byte_end,
            line_start,
            line_end,
            span_sha256,
            type_value,
            project,
            authority,
            confidence,
            status_value,
            valid_from,
            valid_to,
            language,
            title,
            content,
        ) = row
        try:
            ancestry = json.loads(ancestry_json)
        except (TypeError, json.JSONDecodeError):
            return False
        optional_text = (project, authority, confidence, valid_from, valid_to, language)
        expected_chunk = (
            expected_chunks[expected_order]
            if authoritative_sources is not None and expected_order < len(expected_chunks)
            else None
        )
        if (
            not isinstance(chunk_id, str)
            or _SHA256_RE.fullmatch(chunk_id) is None
            or chunk_id in seen_chunk_ids
            or chunk_order != expected_order
            or not isinstance(source_id, str)
            or source_id != f"source:{source_path}"
            or not isinstance(source_path, str)
            or not source_path
            or parent_page != source_path
            or not isinstance(source_sha256, str)
            or _SHA256_RE.fullmatch(source_sha256) is None
            or not isinstance(ancestry, list)
            or any(not isinstance(item, str) for item in ancestry)
            or not isinstance(byte_start, int)
            or not isinstance(byte_end, int)
            or not 0 <= byte_start <= byte_end
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or not 1 <= line_start <= line_end
            or not isinstance(span_sha256, str)
            or _SHA256_RE.fullmatch(span_sha256) is None
            or not isinstance(type_value, str)
            or not type_value
            or any(value is not None and not isinstance(value, str) for value in optional_text)
            or not isinstance(status_value, str)
            or not isinstance(title, str)
            or not isinstance(content, str)
            or not content.strip()
        ):
            return False
        if expected_chunk is not None:
            if row != expected_chunk:
                return False
        seen_chunk_ids.add(chunk_id)
    return len(seen_chunk_ids) == count


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
    filtered: list[dict] = []
    for row in rows:
        path = str(row.get("path") or row.get("relative_path") or "")
        if scope in {"wiki", "memory", "knowledge"} and path and "knowledge/notes/" not in path.replace("\\", "/"):
            continue
        proj = str(row.get("project") or "")
        if project and proj.casefold() != project.casefold():
            continue
        auth = str(row.get("authority") or "")
        if authority and auth.casefold() != authority.casefold():
            continue
        ts = str(row.get("timestamp") or row.get("valid_from") or "")
        status = str(row.get("status") or "").casefold()
        valid_from = str(row.get("valid_from") or "")[:10]
        valid_to = str(row.get("valid_to") or "")[:10]
        if since and ts:
            try:
                if ts[:10] < since[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of:
            if ts:
                try:
                    if ts[:10] > as_of[:10]:
                        continue
                except (IndexError, TypeError):
                    pass
            if valid_from and valid_from > as_of[:10]:
                continue
            if valid_to and valid_to not in {"", "null", "none"} and valid_to <= as_of[:10]:
                continue
        elif status and status not in {"", "active"}:
            continue
        filtered.append(row)
    return filtered


def _generation_result(row: sqlite3.Row, generation_id: str) -> dict[str, object]:
    score = -float(row["rank"])
    authority = row["authority"] or ""
    score *= AUTHORITY_WEIGHTS.get(authority.casefold(), 1.0)
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
    with _generation_sqlite_guard(connection, deadline, cancelled):
        connection.row_factory = sqlite3.Row
        filters, values = _generation_filters(scope=scope, since=since, as_of=as_of)
        normalized_stem = _normalized_filename_stem(query)
        connection.create_function(
            "llm_wiki_filename_stem",
            1,
            lambda value: (
                _normalized_filename_stem(value) if isinstance(value, str) else ""
            ),
            deterministic=True,
        )
        exact_filters = filters
        exact_values = list(values)
        if project:
            exact_filters += " AND lower(project) = lower(?)"
            exact_values.append(project)
        exact_rows = connection.execute(
            "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
            "heading_ancestry, type, project, authority, confidence, status, valid_from, "
            "valid_to, language, title, content, 0.0 AS rank FROM chunks "
            "WHERE llm_wiki_filename_stem(source_path) = ?"
            f"{exact_filters} ORDER BY source_path, chunk_order LIMIT 1",
            [normalized_stem, *exact_values],
        ).fetchall()
        rows = connection.execute(
            "SELECT chunk_id, chunk_order, source_id, source_path, source_sha256, "
            "heading_ancestry, type, project, authority, confidence, status, valid_from, "
            "valid_to, language, title, content, bm25(chunks) AS rank FROM chunks "
            f"WHERE chunks MATCH ?{filters} ORDER BY rank, chunk_order LIMIT ?",
            [_fts_query(query), *values, limit * 5],
        ).fetchall()
    generation_id = str(manifest["generation_id"])
    results = []
    seen_chunk_ids: set[str] = set()
    exact_path = str(exact_rows[0]["source_path"]) if exact_rows else None
    for row in [*exact_rows, *rows]:
        _check_generation_stop(deadline, cancelled)
        chunk_id = str(row["chunk_id"])
        if chunk_id in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_id)
        results.append(_generation_result(row, generation_id))
    query_words = set(query.casefold().split())
    for result in results:
        _check_generation_stop(deadline, cancelled)
        if project and str(result["project"]).casefold() == project.casefold():
            result["score"] = float(result["score"]) * 2.0
        title_words = set(str(result["title"]).casefold().split())
        if query_words and query_words.issubset(title_words):
            result["score"] = float(result["score"]) * 3.0
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
    results = apply_hard_filters(
        results, project=project, since=since, as_of=as_of, scope=scope
    )
    return results[:limit]


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
    _check_generation_stop(deadline, cancelled)
    if (
        manifest.get("vector_state") != "complete"
        or manifest.get("embedding_model_id") != model_id
        or manifest.get("embedding_model_revision") != model_revision
        or not all(_generation_artifact(manifest, name) for name in GENERATION_VECTOR_ARTIFACTS)
    ):
        return None
    generation_id = str(manifest["generation_id"])
    directory = Path(getattr(catalog, "generations_path")) / generation_id
    try:
        import numpy as np

        metadata_bytes = bytearray()
        with (directory / "vectors.json").open("rb") as source:
            while chunk := source.read(64 * 1024):
                _check_generation_stop(deadline, cancelled)
                metadata_bytes.extend(chunk)
        _check_generation_stop(deadline, cancelled)
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        matrix = np.load(directory / "vectors.npy", mmap_mode="r", allow_pickle=False)
        _check_generation_stop(deadline, cancelled)
        with _generation_sqlite_guard(connection, deadline, cancelled):
            connection.row_factory = sqlite3.Row
            ordered = connection.execute(
                "SELECT chunk_id, source_id, source_path, source_sha256 "
                "FROM chunks ORDER BY chunk_order"
            ).fetchall()
        dimensions = manifest.get("vector_dimensions")
        matrix_finite = True
        for start in range(0, len(matrix), 4096):
            _check_generation_stop(deadline, cancelled)
            if not np.isfinite(matrix[start : start + 4096]).all():
                matrix_finite = False
                break
        if (
            metadata.get("schema_version") != "corpus-vectors/v1"
            or metadata.get("corpus_sha256") != manifest.get("source_manifest_sha256")
            or metadata.get("collector_version") != manifest.get("collector_version")
            or metadata.get("extractor_version") != manifest.get("extractor_version")
            or metadata.get("model_id") != model_id
            or metadata.get("model_revision") != model_revision
            or metadata.get("dimensions") != dimensions
            or metadata.get("chunk_ids") != [row[0] for row in ordered]
            or metadata.get("source_ids") != [row[1] for row in ordered]
            or metadata.get("source_paths") != [row[2] for row in ordered]
            or metadata.get("source_sha256") != [row[3] for row in ordered]
            or matrix.shape != (len(ordered), dimensions)
            or matrix.dtype != np.dtype(np.float32)
            or not matrix_finite
        ):
            return None
        _check_generation_stop(deadline, cancelled)
        query_matrix = np.asarray(_call_generation_embedder(embedder, [query]))
        _check_generation_stop(deadline, cancelled)
        if (
            query_matrix.shape != (1, dimensions)
            or query_matrix.dtype.kind != "f"
            or not np.isfinite(query_matrix).all()
        ):
            return None
        query_vector = query_matrix[0]
        query_norm = np.linalg.norm(query_vector) + 1e-10
        similarities = np.empty(len(matrix), dtype=np.float32)
        for start in range(0, len(matrix), 4096):
            _check_generation_stop(deadline, cancelled)
            block = matrix[start : start + 4096]
            similarities[start : start + len(block)] = (block @ query_vector) / (
                (np.linalg.norm(block, axis=1) + 1e-10) * query_norm
            )
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
            if project and str(result["project"]).casefold() == project.casefold():
                score *= 1.5
            result["score"] = round(score, 4)
            result["requested_mode"] = "hybrid"
            result["effective_mode"] = "hybrid"
            results.append(result)
        results.sort(key=lambda item: (-float(item["score"]), str(item["chunk_id"])))
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

    needs_rebuild = force_rebuild
    if not needs_rebuild:
        if deadline is None and cancelled is None:
            needs_rebuild = _needs_rebuild(pages)
        else:
            needs_rebuild = _needs_rebuild(
                pages, deadline=deadline, cancelled=cancelled
            )
    def rebuild() -> None:
        if deadline is None and cancelled is None:
            _build_index(pages)
        else:
            _build_index(pages, deadline=deadline, cancelled=cancelled)

    def query_index() -> list[tuple]:
        _check_legacy_stop(deadline, cancelled)
        connect_timeout = 5.0
        if deadline is not None:
            connect_timeout = max(
                0.0, min(connect_timeout, deadline - time.monotonic())
            )
        conn = sqlite3.connect(str(INDEX_FILE), timeout=connect_timeout)
        try:
            _check_legacy_stop(deadline, cancelled)
            with _legacy_sqlite_guard(conn, deadline, cancelled):
                fts_terms = []
                for word in query.split():
                    if not word:
                        continue
                    safe = word.replace('"', '""')
                    fts_terms.append(f'"{safe}"')
                fts_query = " ".join(fts_terms)
                query_word_count = len([word for word in query.split() if word])
                fetch_multiplier = 5 if query_word_count <= 3 else 3
                return conn.execute(
                    """
                    SELECT path, title, summary, project, timestamp, bm25(pages) as rank
                    FROM pages
                    WHERE pages MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit * fetch_multiplier),
                ).fetchall()
        finally:
            conn.close()

    def direct_fallback() -> list[dict]:
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

    rebuilt = False
    if needs_rebuild:
        try:
            rebuild()
            rebuilt = True
        except sqlite3.DatabaseError:
            return direct_fallback()

    try:
        bm25_raw = query_index()
    except sqlite3.DatabaseError:
        if not rebuilt:
            try:
                rebuild()
                rebuilt = True
            except sqlite3.DatabaseError:
                return direct_fallback()
        try:
            bm25_raw = query_index()
        except sqlite3.DatabaseError:
            return direct_fallback()

    query_lower = query.lower().strip()
    query_words = set(query_lower.split())
    bm25_results: list[dict] = []
    for row in bm25_raw:
        _check_legacy_stop(deadline, cancelled)
        path, title, summary, proj, ts, rank = row
        if since and ts:
            try:
                if ts[:10] < since:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and ts:
            try:
                if ts[:10] > as_of[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and not _valid_as_of(path, as_of):
            continue
        score = -rank
        if project and proj and proj.lower() == project.lower():
            score *= 2.0
        title_lower = (title or "").lower().strip()
        title_words = set(title_lower.split())
        if title_lower == query_lower:
            score *= 5.0
        elif query_words and query_words.issubset(title_words):
            score *= 3.0
        elif title_words and title_words.issubset(query_words):
            score *= 2.0
        filename_slug = Path(path).stem.lower().replace("-", " ")
        if filename_slug == query_lower:
            score *= 10.0
        elif query_words and query_words.issubset(set(filename_slug.split())):
            score *= 4.0
        if "knowledge/notes/" in path:
            score *= 1.3
        score *= _authority_weight(path)
        bm25_results.append(
            {
                "path": path,
                "title": title,
                "summary": summary[:120] if summary else "",
                "score": score,
                "bm25_score": score,
                "project": proj or "",
                "timestamp": ts or "",
                "candidate_id": Path(path).stem,
                "generation": "legacy",
            }
        )
    normalized_stem = _normalized_filename_stem(query)
    exact_page = next(
        (page for page in pages if _normalized_filename_stem(page.name) == normalized_stem),
        None,
    )
    if exact_page is not None:
        try:
            relative_path = exact_page.relative_to(ROOT).as_posix()
            raw = read_stable_bytes(
                exact_page, MAX_PAGE_BYTES, label="exact filename search page"
            )
            content = raw.decode("utf-8", errors="ignore")
        except (OSError, ValueError):
            exact_page = None
        else:
            title, summary = _extract_title_and_summary(content, exact_page.stem)
            page_project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
            timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
            timestamp = timestamp[:10] if timestamp else ""
            eligible = not project or page_project.casefold() == project.casefold()
            eligible = eligible and (not since or not timestamp or timestamp >= since[:10])
            eligible = eligible and (not as_of or _valid_as_of(relative_path, as_of))
            if eligible and all(item["path"] != relative_path for item in bm25_results):
                authority = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE) or ""
                bm25_results.append(
                    {
                        "path": relative_path,
                        "title": title,
                        "summary": summary[:120],
                        "score": round(
                            10.0 * AUTHORITY_WEIGHTS.get(authority.casefold(), 1.0),
                            2,
                        ),
                        "bm25_score": 0.0,
                        "project": page_project,
                        "timestamp": timestamp,
                        "candidate_id": exact_page.stem,
                        "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "byte_start": 0,
                        "byte_end": len(raw),
                        "generation": "legacy",
                        "authority": authority,
                    }
                )
    bm25_results.sort(key=lambda x: (-x["score"], x["path"]))
    for result in bm25_results:
        result["score"] = round(float(result["score"]), 2)
        result["bm25_score"] = round(float(result["bm25_score"]), 2)
    # Prefer filename exact matches first while remaining a pure ranked list.
    filename_matches = [
        result
        for result in bm25_results
        if _normalized_filename_stem(str(result["path"])) == normalized_stem
    ]
    if filename_matches:
        filename_matches.sort(
            key=lambda r: (
                0 if "knowledge/notes/" in r["path"] else 1,
                -r["score"],
                r["path"],
            )
        )
        best = filename_matches[0]
        rest = [x for x in bm25_results if x["path"] != best["path"]]
        bm25_results = [best] + rest
    return bm25_results[: max(limit * 3, limit)]


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
    query_terms = tuple(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    query_term_set = set(query_terms)
    results: list[dict] = []
    for page in pages:
        _check_legacy_stop(deadline, cancelled)
        try:
            relative_path = page.relative_to(ROOT).as_posix()
            raw = read_stable_bytes(page, MAX_PAGE_BYTES, label="search page")
            content = raw.decode("utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        title, summary = _extract_title_and_summary(content, page.stem)
        body = _strip_frontmatter(content)
        document_terms = set(
            re.findall(
                r"\w+",
                f"{page.stem.replace('-', ' ')} {title} {summary} {body}".casefold(),
            )
        )
        if not query_terms or not query_term_set.issubset(document_terms):
            continue
        page_project = _extract_frontmatter_field(content, PROJECT_FIELD_RE) or ""
        if project and page_project.casefold() != project.casefold():
            continue
        timestamp = _extract_frontmatter_field(content, TIMESTAMP_FIELD_RE) or ""
        timestamp = timestamp[:10] if timestamp else ""
        if since and timestamp and timestamp < since[:10]:
            continue
        if as_of and not _valid_as_of(relative_path, as_of):
            continue
        score = float(len(query_terms))
        title_terms = set(re.findall(r"\w+", title.casefold()))
        filename_terms = set(re.findall(r"\w+", page.stem.casefold()))
        if query_term_set.issubset(title_terms):
            score *= 3.0
        if query_term_set.issubset(filename_terms):
            score *= 4.0
        authority = _extract_frontmatter_field(content, AUTHORITY_FIELD_RE) or ""
        score *= AUTHORITY_WEIGHTS.get(authority.casefold(), 1.0)
        results.append(
            {
                "path": relative_path,
                "title": title,
                "summary": summary[:120],
                "score": round(score, 2),
                "bm25_score": round(score, 2),
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
        )
    results.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
    if not results:
        raise LegacySearchUnavailable(
            "legacy SQLite unavailable and direct Markdown search found no matching page"
        )
    return results[: max(limit * 3, limit)]


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
    if not query or not query.strip():
        return None
    if not _have_sentence_transformers():
        return None
    pages = (
        page_paths
        if page_paths is not None
        else _collect_pages(scope, deadline=deadline or float("inf"))
    )
    if not pages:
        return None
    vector_results = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lance_store import have_lancedb
        from lance_store import vector_search as _lance_search

        if have_lancedb():
            qvecs = _embed_texts(
                [query], is_query=True, deadline=deadline, cancelled=cancelled
            )
            if qvecs:
                lance_results = _lance_search(
                    qvecs[0],
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
                if lance_results:
                    vector_results = []
                    for item in lance_results:
                        row = dict(item)
                        row["vector_score"] = row.get("vector_score", row.get("score"))
                        row["candidate_id"] = Path(str(row.get("path") or "")).stem
                        row["generation"] = "legacy"
                        vector_results.append(row)
    except TimeoutError:
        raise
    except Exception:
        vector_results = None
    if vector_results is None:
        try:
            vector_results = _vector_search(
                query,
                pages,
                limit * 3,
                project,
                since,
                as_of,
                deadline=deadline,
                cancelled=cancelled,
            )
            if vector_results is not None:
                for row in vector_results:
                    row["vector_score"] = row.get("score")
                    row.setdefault("candidate_id", Path(str(row.get("path") or "")).stem)
                    row["generation"] = "legacy"
        except TimeoutError:
            raise
        except Exception:
            return None
    return vector_results


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
    stop_options = {}
    if deadline is not None:
        stop_options["deadline"] = deadline
    if cancelled is not None:
        stop_options["cancelled"] = cancelled
    _check_generation_stop(deadline, cancelled)
    if selected_catalog is not None and not force_rebuild and page_paths is None:
        try:
            manifest = selected_catalog.get_active(**stop_options)
        except TimeoutError:
            raise
        except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
            manifest = None
        if isinstance(manifest, dict):
            artifact_names = (GENERATION_FTS_ARTIFACT,)
            vector_requested = (
                semantic
                and generation_embedder is not None
                and generation_model_id is not None
                and generation_model_revision is not None
                and manifest.get("vector_state") == "complete"
            )
            if vector_requested:
                artifact_names += GENERATION_VECTOR_ARTIFACTS
            consumption_seal = _generation_consumption_seal(
                selected_catalog, manifest, artifact_names, **stop_options
            )
            connection = (
                _generation_connection(selected_catalog, manifest, **stop_options)
                if consumption_seal is not None
                else None
            )
            if connection is not None:
                try:
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
                    if semantic:
                        vectors = None
                        if (
                            generation_embedder is not None
                            and generation_model_id is not None
                            and generation_model_revision is not None
                        ):
                            vectors = _generation_vectors_search(
                                query,
                                selected_catalog,
                                manifest,
                                connection,
                                embedder=generation_embedder,
                                model_id=generation_model_id,
                                model_revision=generation_model_revision,
                                scope=scope,
                                limit=limit,
                                project=project,
                                since=since,
                                as_of=as_of,
                                **stop_options,
                            )
                        if vectors is not None:
                            if _generation_consumption_unchanged(
                                selected_catalog,
                                manifest,
                                artifact_names,
                                consumption_seal,
                                **stop_options,
                            ):
                                return _finalize_generation_results(
                                    _fuse_generation_results(lexical, vectors, limit),
                                    query=query,
                                    source_tool=source_tool,
                                    emit_telemetry=emit_telemetry,
                                )
                            vectors = None
                        for result in lexical:
                            _check_generation_stop(deadline, cancelled)
                            result["requested_mode"] = "hybrid"
                            result["fallback_reason"] = "generation_vectors_unavailable"
                    if _generation_consumption_unchanged(
                        selected_catalog,
                        manifest,
                        artifact_names,
                        consumption_seal,
                        **stop_options,
                    ):
                        return _finalize_generation_results(
                            lexical,
                            query=query,
                            source_tool=source_tool,
                            emit_telemetry=emit_telemetry,
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
                    pass
                finally:
                    connection.close()
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
    """Run a hybrid BM25 + optional vector search.

    Optional filters:
    - project: boost results tagged with `project: <slug>` (x2 score boost)
    - since: only results with timestamp >= YYYY-MM-DD
    - as_of: only results valid on YYYY-MM-DD (timestamp <= as_of and
      valid_to empty or >= as_of); also applies source_authority weights
    - semantic: if sentence-transformers is installed, also run vector
      search and fuse results via RRF. Finds semantically related pages
      even when keywords don't match.
    """
    if not query or not query.strip():
        return []

    pages = page_paths if page_paths is not None else _collect_pages(scope)
    if not pages:
        return []

    if force_rebuild or _needs_rebuild(pages):
        _build_index(pages)

    conn = sqlite3.connect(str(INDEX_FILE))

    # BM25 search (always runs)
    # Escape FTS5 special tokens: wrap each word in double quotes to
    # prevent FTS5 from interpreting common words (in, not, and, or,
    # near) as operators or column names. This preserves AND semantics
    # between terms while avoiding syntax errors.
    fts_terms = []
    for w in query.split():
        if not w:
            continue
        safe = w.replace('"', '""')
        fts_terms.append(f'"{safe}"')
    fts_query = " ".join(fts_terms)
    # Adaptive fetch limit: short queries (≤3 words) match more pages,
    # so fetch more candidates to give filename/title boosts a chance.
    query_word_count = len([w for w in query.split() if w])
    fetch_multiplier = 5 if query_word_count <= 3 else 3
    bm25_raw = conn.execute(
        """
        SELECT path, title, summary, project, timestamp, bm25(pages) as rank
        FROM pages
        WHERE pages MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit * fetch_multiplier),
    ).fetchall()
    conn.close()

    # TITLE BOOST: if a page's title matches the query, boost its score.
    # This fixes Recall@1 regressions where a duplicate page (promoted
    # wiki copy) outscores the original knowledge page.
    query_lower = query.lower().strip()
    query_words = set(query_lower.split())

    bm25_results = []
    for row in bm25_raw:
        path, title, summary, proj, ts, rank = row
        if since and ts:
            try:
                if ts[:10] < since:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and ts:
            try:
                if ts[:10] > as_of[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and not _valid_as_of(path, as_of):
            continue
        score = -rank
        if project and proj and proj.lower() == project.lower():
            score *= 2.0

        # Title boost (highest impact on Recall@1)
        title_lower = (title or "").lower().strip()
        title_words = set(title_lower.split())
        if title_lower == query_lower:
            # Exact title match → massive boost
            score *= 5.0
        elif query_words and query_words.issubset(title_words):
            # All query words are in the title → strong boost
            score *= 3.0
        elif title_words and title_words.issubset(query_words):
            # Title is a subset of query → moderate boost
            score *= 2.0

        # FILENAME MATCH BOOST: if the query matches the filename slug,
        # this is almost certainly the right page. Strongest precision signal.
        # "hook scripts defense-in-depth" → filename "hook-scripts-defense-in-depth"
        filename_slug = Path(path).stem.lower().replace("-", " ")
        if filename_slug == query_lower:
            score *= 10.0  # near-guaranteed correct match
        elif query_words and query_words.issubset(set(filename_slug.split())):
            score *= 4.0

        # Path preference: knowledge/notes/ is the canonical durable-pages
        # tree. (Pre-three-zone this distinguished wiki/ from memory/; both
        # now resolve to the same knowledge/notes path, so the boost is a
        # no-op kept for forward-compat if a second tree is reintroduced.)
        if "knowledge/notes/" in path:
            score *= 1.3  # increased from 1.2 to break ties more decisively

        # Typed provenance: user-said outranks inferred/ai-derived.
        score *= _authority_weight(path)

        bm25_results.append({
            "path": path,
            "title": title,
            "summary": summary[:120] if summary else "",
            "score": round(score, 2),
            "project": proj or "",
            "timestamp": ts or "",
        })

    # RE-SORT after boosts! FTS5 returns results in bm25() order, but
    # title/filename boosts change the effective score. Without this
    # re-sort, a page boosted to score=300 stays at its original FTS5
    # position (e.g. rank 2) even though it should be rank 1.
    bm25_results.sort(key=lambda x: x["score"], reverse=True)

    # SHORT-CIRCUIT: if any page has exact filename match with the query,
    # return it at rank 1 immediately. This prevents graph-neighbor RRF
    # from pushing a filename-matched page down by promoting a linked
    # but incorrect page (e.g. wiki copy beating the knowledge original).
    # When multiple pages match (duplicates), prefer knowledge/notes/.
    query_normalized = query.lower().strip().replace(" ", "-")
    filename_matches = [
        r for r in bm25_results[:10]
        if Path(r["path"]).stem.lower() == query_normalized
    ]
    if filename_matches:
        # Sort matches: knowledge/notes/ first (primary source),
        # then by score (highest first)
        filename_matches.sort(
            key=lambda r: (
                0 if "knowledge/notes/" in r["path"] else 1,
                -r["score"],
            )
        )
        best = filename_matches[0]
        rest = [x for x in bm25_results if x["path"] != best["path"]][:limit-1]
        return _finalize_results(
            query,
            [best] + rest,
            limit,
            retrieval_mode="exact",
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
        )

    # Optional: vector search for semantic matching
    vector_results = None
    if semantic and _have_sentence_transformers():
        # Try LanceDB (HNSW index) first, fall back to numpy brute-force.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from lance_store import have_lancedb
            from lance_store import vector_search as _lance_search

            if have_lancedb():
                qvecs = _embed_texts([query], is_query=True)
                if qvecs:
                    lance_results = _lance_search(
                        qvecs[0], limit * 3, project, since=since, as_of=as_of
                    )
                    if lance_results:
                        vector_results = lance_results
        except Exception:
            pass

        # Fall back to numpy brute-force if LanceDB unavailable or empty.
        if vector_results is None:
            try:
                vector_results = _vector_search(query, pages, limit * 3, project, since, as_of)
            except Exception as e:
                print(f"  (vector search failed: {e})", file=sys.stderr)
                vector_results = None

    # Optional: graph-neighbor boost (3rd retrieval signal)
    graph_boosts = None
    if graph:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from graph_neighbors import boost_graph_neighbors
            graph_boosts = boost_graph_neighbors(bm25_results, vector_results)
        except Exception:
            pass

    # Fuse results: BM25 + Vector + Graph-neighbor via RRF
    if vector_results or graph_boosts:
        fused = _rrf_fuse_triple(bm25_results, vector_results, graph_boosts)
        # Apply project boost on fused results
        if project:
            for r in fused:
                if r.get("project", "").lower() == project.lower():
                    r["fused_score"] = round(r["fused_score"] * 1.5, 4)
            fused.sort(key=lambda x: x.get("fused_score", 0), reverse=True)
        final = _maybe_rerank(query, fused, limit) if rerank else fused[:limit]
        return _finalize_results(
            query,
            final,
            limit,
            retrieval_mode="hybrid",
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
        )

    # BM25 only (fallback)
    bm25_results.sort(key=lambda x: x["score"], reverse=True)
    final = _maybe_rerank(query, bm25_results, limit) if rerank else bm25_results[:limit]
    return _finalize_results(
        query,
        final,
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
    """Run vector similarity search using sentence-transformers.

    Builds embeddings for all pages (cached) and the query, then
    returns pages ranked by cosine similarity.
    """
    import numpy as np

    # Load or build vector cache
    _check_legacy_stop(deadline, cancelled)
    vectors_data = _load_or_build_vectors(
        pages, deadline=deadline, cancelled=cancelled
    )
    if not vectors_data:
        return None

    paths = vectors_data["paths"]
    titles = vectors_data["titles"]
    summaries = vectors_data["summaries"]
    projects = vectors_data["projects"]
    timestamps = vectors_data["timestamps"]
    vectors = vectors_data["vectors"]
    if isinstance(vectors, list):
        # Refuse list-converted caches; require ndarray/mmap.
        return None
    matrix = np.asarray(vectors)
    if matrix.ndim != 2 or matrix.shape[0] != len(paths):
        return None

    # Embed the query
    query_vec = _embed_texts(
        [query], is_query=True, deadline=deadline, cancelled=cancelled
    )
    if not query_vec:
        return None
    q = np.asarray(query_vec[0], dtype=np.float32)
    if q.shape[0] != matrix.shape[1] or not np.isfinite(q).all():
        return None

    # Compute cosine similarity without materializing full Python lists.
    sims = _cosine_similarity(q, matrix)

    # Build results
    results = []
    for i, sim in enumerate(sims):
        _check_legacy_stop(deadline, cancelled)
        proj = projects[i]
        ts = timestamps[i]
        path = paths[i]
        if since and ts:
            try:
                if ts[:10] < since:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and ts:
            try:
                if ts[:10] > as_of[:10]:
                    continue
            except (IndexError, TypeError):
                pass
        if as_of and not _valid_as_of(path, as_of):
            continue
        # status filter parity when frontmatter status is superseded and no as_of
        if not as_of:
            try:
                p = ROOT / path if not Path(path).is_absolute() else Path(path)
                content = p.read_text(encoding="utf-8", errors="ignore")
                status = (_extract_frontmatter_field(content, re.compile(
                    r"^status:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
                )) or "active").strip().lower()
                if status and status not in {"active", ""}:
                    continue
            except OSError:
                pass
        score = round(float(sim), 4)
        if project and proj and proj.lower() == project.lower():
            score = round(score * 1.5, 4)
        results.append({
            "path": path,
            "title": titles[i],
            "summary": (summaries[i] or "")[:120],
            "score": score,
            "project": proj,
            "timestamp": ts,
            "candidate_id": Path(path).stem,
        })

    results.sort(key=lambda x: (-x["score"], x["path"]))
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
