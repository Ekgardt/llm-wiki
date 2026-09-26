"""Hybrid search over the vault's knowledge.

Uses Python's built-in sqlite3 + FTS5 for BM25 full-text search over the active
generation. When the `semantic` extra is installed, the pinned E5 encoder runs
through ONNX Runtime (`scripts/onnx_encoder.py`) for the dense leg. Results are
fused via Reciprocal Rank Fusion (RRF) for hybrid ranking.

Costs measured on this vault:
- lexical only: tens of milliseconds, zero optional dependencies
- with vectors: loading the encoder costs about 1.5 s once per process
  (2026-09-25; it was 6.5 s through `sentence-transformers` and `torch`), and
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
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes  # noqa: E402
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
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from page_status import current_status_sql, is_retired  # noqa: E402
from provenance import substance_weight, trust_weight  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    validate_runtime_file,
)
from secret_redact import redact_secrets  # noqa: E402

_INDEX_REPLACE_WAIT_SECONDS = 1.0
MAX_SEARCHABLE_PAGES = 10_000
MAX_SEARCH_ENTRIES = 20_000
MAX_SEARCH_DIRECTORIES = 2_000
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_LIMIT = 1_000
MAX_PAGE_BYTES = MAX_KNOWLEDGE_PAGE_BYTES
SEARCH_INDEX_COLUMNS = (
    "path", "title", "summary", "body", "project", "timestamp", "slug",
)
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

# The model, its revision and its prefixes live in one module, because the
# generation builder encodes with the same model and a drifting copy would
# embed questions and pages with different ones.
from embedding_model import (  # noqa: E402
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_REVISION,
    prefixed_texts,
)

# Why there is no dense signal, in three words a reader can act on:
# `import_failed` (the optional package is not installed — a worker launched
# under the system interpreter instead of `uv run`), `model_unavailable` (the
# package is there, the weights are not) and `load_failed` (both are there and
# the model still would not load). The reason is bounded and redacted because
# a load error quotes paths and occasionally a token.
EMBEDDER_REASON_MAX_CHARS = 200

_embedder_unavailable_reason: str | None = None
_embedder_announced: set[str] = set()


def embedder_unavailable_reason() -> str | None:
    """Why the embedding model is unavailable, or None when it loaded.

    `_get_embedder` was `except Exception: return None`, so a missing package,
    a missing model and a corrupt one were the same answer as a vault that
    simply has no vectors yet. A LongMemEval worker run under the system
    `python3` retrieved zero rows and looked exactly like a retrieval
    regression; the generation carried `vector_state: absent`, which was true
    and said nothing.
    """
    return _embedder_unavailable_reason


def _embedder_failure_kind(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        return "import_failed"
    if isinstance(exc, OSError):
        return "model_unavailable"
    return "load_failed"


# One reason per kind of degraded retrieval stage, said once on stderr and
# readable by the health resource; the model-load reason above was the first
# of these. Research: docs/research/2026-09-10-a-silent-fallback-names-its-cause.md
_DEGRADATIONS: dict[str, str] = {}
_degradations_announced: set[str] = set()


def note_degradation(kind: str, error: BaseException) -> None:
    """Record why a retrieval stage fell back, as `Class: redacted message`."""
    from secret_redact import describe_error

    reason = describe_error(error)[:EMBEDDER_REASON_MAX_CHARS]
    _DEGRADATIONS[kind] = reason
    if kind in _degradations_announced:
        return
    _degradations_announced.add(kind)
    print(f"search_memory: {kind} degraded — {reason}", file=sys.stderr)


def degradation_reasons() -> dict[str, str]:
    return dict(_DEGRADATIONS)


def _note_embedder_unavailable(kind: str, detail: str) -> None:
    """Record why there is no dense signal, and say it once, not per call."""
    global _embedder_unavailable_reason
    text = " ".join(redact_secrets(detail).split())[:EMBEDDER_REASON_MAX_CHARS]
    _embedder_unavailable_reason = f"{kind}: {text}" if text else kind
    if kind in _embedder_announced:
        return
    _embedder_announced.add(kind)
    print(
        f"search_memory: no dense signal — embedding model {EMBEDDING_MODEL} "
        f"is unavailable ({_embedder_unavailable_reason})",
        file=sys.stderr,
    )


ENCODER_PACKAGES = ("onnxruntime", "tokenizers", "huggingface_hub", "numpy")


def _have_encoder_runtime() -> bool:
    """Whether the encoder's packages are installed, without importing them."""
    import importlib.util

    missing = [name for name in ENCODER_PACKAGES if importlib.util.find_spec(name) is None]
    if missing:
        _note_embedder_unavailable("import_failed", f"not installed: {', '.join(missing)}")
    return not missing


def _get_embedder():
    """Lazily load the embedding model. Returns None if unavailable.

    The model is cached at module level: the 470 MB session is built once per
    process, not per query.

    Unavailable still means None, never an exception: the generation reader
    treats an unusable query vector as "no dense signal" by contract and that
    is exactly what an unavailable model means. What changed is that the
    reason is recorded and named — see `embedder_unavailable_reason`.

    The load is local-only: the files come from the Hugging Face cache and
    never the network (`scripts/install_models.py` fetches them). A vault that
    does not have the weights says `model_unavailable` and degrades to lexical,
    which is the same answer it gives for weights it cannot read.
    """
    global _embedder_cache, _embedder_unavailable_reason
    if _embedder_cache is not None:
        return _embedder_cache
    try:
        from onnx_encoder import load_encoder

        _embedder_cache = load_encoder(EMBEDDING_MODEL, EMBEDDING_MODEL_REVISION)
    except Exception as exc:  # noqa: BLE001 - no dense signal is not an error
        _note_embedder_unavailable(
            _embedder_failure_kind(exc), f"{type(exc).__name__}: {exc}"
        )
        return None
    _embedder_unavailable_reason = None
    return _embedder_cache


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
                keys,
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


def _keys_of(chunk: object, keys: Mapping[str, str] | None) -> str:
    """The fact keys the nightly pass wrote for this chunk's turn, or nothing."""
    if not keys:
        return ""
    return keys.get(chunk.span_sha256, "")


def _write_generation_fts(
    database: sqlite3.Connection,
    snapshot: CorpusSnapshot,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    keys: Mapping[str, str] | None = None,
    ledger_rows: Sequence[tuple[object, ...]] | None = None,
) -> None:
    """Schema, metadata and every chunk, verified before the caller publishes.

    `ledger_rows` is the nightly ledger of things and events, carried as one more
    table of this artifact (`ledger.write_table`) so a count is made by code over
    every record, sealed by the artifact's own digest; None means the build
    carried no ledger and a reader finds no table. Research:
    `docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
    """
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
            yield (*_generation_chunk_row(chunk, order), _keys_of(chunk, keys))

    database.executemany(
        "INSERT INTO chunks VALUES (" + ",".join("?" for _ in range(23)) + ")",
        rows(),
    )
    _write_generation_ledger(database, ledger_rows)
    database.commit()
    if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ValueError("generation FTS integrity check failed")


def _write_generation_ledger(
    database: sqlite3.Connection, ledger_rows: Sequence[tuple[object, ...]] | None
) -> None:
    if ledger_rows is None:
        return
    import ledger

    ledger.write_table(database, ledger_rows)


def _stop_reached(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    if cancelled is not None and cancelled():
        return True
    return deadline is not None and time.monotonic() >= deadline


def _require_buildable_snapshot(
    snapshot: object,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    _check_generation_stop(deadline, cancelled)
    if len(snapshot.chunks) > MAX_GENERATION_FTS_CHUNKS:
        raise ValueError("generation FTS chunk row ceiling exceeded")


def _discard_unfinished_fts(destination: Path, temporary: Path, *, complete: bool) -> None:
    if not complete:
        _remove_quietly((destination,))
    _remove_quietly((temporary,))


def _built_fts_artifact(
    snapshot: CorpusSnapshot,
    directory: Path,
    destination: Path,
    temporary: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    keys: Mapping[str, str] | None = None,
    ledger_rows: Sequence[tuple[object, ...]] | None = None,
) -> dict[str, object]:
    """Write the artifact under a temporary name, then publish it atomically."""
    state = {"stopped": False, "complete": False}

    def progress() -> int:
        state["stopped"] = _stop_reached(deadline, cancelled)
        return int(state["stopped"])

    try:
        with closing(sqlite3.connect(temporary)) as database:
            database.set_progress_handler(progress, GENERATION_FTS_PROGRESS_OPCODES)
            _write_generation_fts(
                database,
                snapshot,
                deadline=deadline,
                cancelled=cancelled,
                keys=keys,
                ledger_rows=ledger_rows,
            )
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
        state["complete"] = True
        return descriptor
    except sqlite3.DatabaseError as exc:
        if state["stopped"]:
            raise TimeoutError(
                "generation FTS build cancelled or deadline reached"
            ) from exc
        raise
    finally:
        _discard_unfinished_fts(destination, temporary, complete=state["complete"])


def build_generation_fts(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    keys: Mapping[str, str] | None = None,
    ledger_rows: Sequence[tuple[object, ...]] | None = None,
) -> dict[str, object]:
    """Build one immutable generation-local FTS5 artifact from captured chunks.

    `keys` carries the nightly fact keys per turn span; they are indexed beside the chunk
    and never returned to a reader. Research:
    `docs/research/2026-09-16-the-keys-are-indexed-beside-the-turn.md`.
    `ledger_rows` carries the nightly ledger of things and events as one more table.
    """
    _require_buildable_snapshot(snapshot, deadline, cancelled)
    directory = _generation_directory(generation_directory)
    destination = directory / GENERATION_FTS_ARTIFACT
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    temporary = directory / f".{GENERATION_FTS_ARTIFACT}.{uuid.uuid4().hex}.tmp"
    return _built_fts_artifact(
        snapshot,
        directory,
        destination,
        temporary,
        deadline=deadline,
        cancelled=cancelled,
        keys=keys,
        ledger_rows=ledger_rows,
    )


def _call_generation_embedder(embedder: object, texts: list[str]):
    if callable(embedder):
        return embedder(texts)
    encode = getattr(embedder, "encode", None)
    if not callable(encode):
        raise TypeError("embedder must be callable or provide encode()")
    return encode(texts)


def _require_positive_int(value: object, message: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(message)


def _require_vector_build_inputs(
    snapshot: object, model_id: str, model_revision: str, dimensions: int
) -> None:
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if not model_id or not model_revision:
        raise ValueError("model ID and revision must be non-empty")
    _require_positive_int(dimensions, "dimensions must be a positive integer")


def _require_absent_artifacts(destinations: list[Path]) -> None:
    for destination in destinations:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)


def _require_embedded_shape(matrix, rows: int, dimensions: int) -> None:
    import numpy as np

    if matrix.ndim != 2 or matrix.shape != (rows, dimensions):
        raise ValueError("embedder returned a matrix with incompatible shape")
    if matrix.dtype.kind not in "fiu" or not np.isfinite(matrix).all():
        raise ValueError("embedder returned a non-finite numeric matrix")


# Texts per embedding call, with a stop check before each: `encode` cannot be
# interrupted, and one call over a first build's every chunk ran a 900-second pass
# past 1 000 s. See `docs/research/2026-09-14-a-pass-that-keeps-its-budget.md`.
EMBED_STOP_BATCH = 256


def _embedded_block(texts: list[str], embedder: object, dimensions: int, check_stop):
    import numpy as np

    if check_stop is not None:
        check_stop()
    matrix = np.asarray(_call_generation_embedder(embedder, texts))
    _require_embedded_shape(matrix, len(texts), dimensions)
    return matrix


def _embedded_rows(texts: list[str], embedder: object, dimensions: int, check_stop=None):
    """Encode exactly the texts asked for, or return an empty (0, d) block."""
    import numpy as np

    if not texts:
        return np.zeros((0, dimensions), dtype=np.float32)
    starts = range(0, len(texts), EMBED_STOP_BATCH)
    blocks = [
        _embedded_block(texts[start : start + EMBED_STOP_BATCH], embedder, dimensions, check_stop)
        for start in starts
    ]
    return np.ascontiguousarray(np.vstack(blocks), dtype=np.float32)


def _reused_matrix(
    snapshot: CorpusSnapshot,
    embedder: object,
    dimensions: int,
    cache: Mapping[str, object],
    check_stop=None,
) -> tuple[object, int]:
    """One row per chunk, taken from the cache where the chunk digest matches.

    A chunk ID is `sha256({extractor, parent, path, range, sha256})` where the
    last field is the digest of the chunk's own bytes and `chunk.text` is those
    bytes decoded — so an equal ID means equal text, and the model has already
    been checked to be the same model at the same revision. The reused row is
    therefore the row that model produced for that exact text.

    What this does *not* claim: that the row equals what a fresh full build
    would emit here. It does not, and neither does another full build — measured
    on 400 real chunks, re-batching the same texts moves float32 results by up
    to 8.6e-08, because the encoder pads and sorts by length. See
    `docs/research/2026-08-28-what-a-rebuild-may-reuse.md`.
    """
    import numpy as np

    fresh_positions = [
        index for index, chunk in enumerate(snapshot.chunks) if chunk.id not in cache
    ]
    fresh = _embedded_rows(
        [snapshot.chunks[index].text for index in fresh_positions], embedder, dimensions, check_stop
    )
    matrix = np.zeros((len(snapshot.chunks), dimensions), dtype=np.float32)
    for row, index in enumerate(fresh_positions):
        matrix[index] = fresh[row]
    _fill_cached_rows(matrix, snapshot, cache)
    return matrix, len(snapshot.chunks) - len(fresh_positions)


def _fill_cached_rows(matrix, snapshot: CorpusSnapshot, cache: Mapping[str, object]) -> None:
    for index, chunk in enumerate(snapshot.chunks):
        cached = cache.get(chunk.id)
        if cached is None:
            continue
        matrix[index] = cached


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


#: The identity a cached vector is namespaced by. A row is reusable only when
#: every one of these matches, on top of the chunk digest itself.
VECTOR_CACHE_IDENTITY_KEYS = ("schema_version", "model_id", "model_revision", "dimensions")
#: Bounds the read of a parent generation's vector metadata. It holds four
#: parallel ID lists, so it grows with the number of chunks, not with history.
MAX_PARENT_VECTOR_METADATA_BYTES = 256 * 1024 * 1024


def _parent_vector_metadata(reuse_from: Path) -> Mapping[str, object] | None:
    metadata_path = reuse_from / "vectors.json"
    matrix_path = reuse_from / "vectors.npy"
    if not metadata_path.is_file() or not matrix_path.is_file():
        return None
    if metadata_path.stat().st_size > MAX_PARENT_VECTOR_METADATA_BYTES:
        return None
    return _json_mapping(_sealed_parent_bytes(reuse_from, "vectors.json"))


def _parent_artifact_digest(reuse_from: Path, name: str) -> str | None:
    """The SHA-256 the parent's sealed manifest records for one artifact."""
    descriptors = _artifact_descriptors(_loaded_json_mapping(reuse_from / "manifest.json") or {})
    sealed = (descriptors or {}).get(name, {}).get("sha256")
    return sealed if isinstance(sealed, str) else None


def _sealed_parent_bytes(reuse_from: Path, name: str) -> bytes | None:
    """The artifact's bytes, only when they hash to the parent manifest's seal.

    The builder used to trust whatever sat in the parent directory; a damaged matrix of
    the right shape was copied and sealed again. See
    `docs/research/2026-09-14-reused-vectors-match-their-seal.md`.
    """
    expected = _parent_artifact_digest(reuse_from, name)
    if expected is None:
        return None
    try:
        data = (reuse_from / name).read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == expected else None


def _json_mapping(data: bytes | None) -> Mapping[str, object] | None:
    if data is None:
        return None
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _loaded_json_mapping(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    return value


def _vector_identity_matches(
    metadata: Mapping[str, object], model_id: str, model_revision: str, dimensions: int
) -> bool:
    expected = {
        "schema_version": "corpus-vectors/v1",
        "model_id": model_id,
        "model_revision": model_revision,
        "dimensions": dimensions,
    }
    return all(metadata.get(key) == expected[key] for key in VECTOR_CACHE_IDENTITY_KEYS)


def _loaded_parent_matrix(reuse_from: Path, rows: int, dimensions: int):
    import io

    import numpy as np

    data = _sealed_parent_bytes(reuse_from, "vectors.npy")
    if data is None:
        return None
    try:
        matrix = np.load(io.BytesIO(data), allow_pickle=False)
    except (OSError, ValueError):
        return None
    if matrix.shape != (rows, dimensions) or matrix.dtype != np.float32:
        return None
    return matrix


def _reusable_vector_rows(
    reuse_from: Path | None, model_id: str, model_revision: str, dimensions: int
) -> dict[str, object]:
    """The parent generation's vectors, keyed by chunk digest.

    The parent generation *is* the cache — it is already immutable, already
    carries one digest per chunk beside its matrix, and is already pruned by the
    catalog's own retention. Nothing new is stored, so nothing new can grow
    without bound.

    Every refusal here is silent and total: an unusable parent means this build
    embeds everything, which is exactly what every build did before.
    """
    if reuse_from is None:
        return {}
    metadata = _parent_vector_metadata(reuse_from)
    if metadata is None or not _vector_identity_matches(
        metadata, model_id, model_revision, dimensions
    ):
        return {}
    return _rows_by_chunk_id(reuse_from, metadata, dimensions)


def _rows_by_chunk_id(
    reuse_from: Path, metadata: Mapping[str, object], dimensions: int
) -> dict[str, object]:
    chunk_ids = metadata.get("chunk_ids")
    if not _usable_chunk_ids(chunk_ids):
        return {}
    matrix = _loaded_parent_matrix(reuse_from, len(chunk_ids), dimensions)
    if matrix is None:
        return {}
    return {
        chunk_id: matrix[index]
        for index, chunk_id in enumerate(chunk_ids)
        if isinstance(chunk_id, str)
    }


def _usable_chunk_ids(chunk_ids: object) -> bool:
    """Duplicate IDs would make a row ambiguous, so the whole cache is refused."""
    if not isinstance(chunk_ids, list):
        return False
    return len(set(chunk_ids)) == len(chunk_ids)


def build_generation_numpy_vectors(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    dimensions: int,
    reuse_from: Path | None = None,
) -> list[dict[str, object]]:
    """Build an exact NumPy matrix and closed metadata from one chunk sequence."""
    return _built_generation_vectors(
        snapshot,
        generation_directory,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
        dimensions=dimensions,
        reuse_from=reuse_from,
    )[0]


def _built_generation_vectors(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    dimensions: int,
    reuse_from: Path | None = None,
    check_stop=None,
) -> tuple[list[dict[str, object]], int]:
    """The same build, plus how many rows came from the parent rather than the model."""
    _require_vector_build_inputs(snapshot, model_id, model_revision, dimensions)
    directory = _generation_directory(generation_directory)
    destinations = [directory / name for name in GENERATION_VECTOR_ARTIFACTS]
    _require_absent_artifacts(destinations)
    cache = _reusable_vector_rows(reuse_from, model_id, model_revision, dimensions)
    matrix, reused = _reused_matrix(snapshot, embedder, dimensions, cache, check_stop)
    _publish_vector_artifacts(
        directory,
        destinations,
        _vector_metadata(
            snapshot,
            model_id=model_id,
            model_revision=model_revision,
            dimensions=dimensions,
        ),
        matrix,
    )
    return [
        _artifact_descriptor(directory / name, name)
        for name in GENERATION_VECTOR_ARTIFACTS
    ], reused


def _generation_embedder(embedder, *, is_query: bool):
    """The generation paths want a callable, and E5 wants each side told apart.

    A page is a passage and a question is a query; encoding either with the
    other's prefix costs accuracy the model was trained to give.
    """

    def encode(texts) -> list[list[float]]:
        return embedder.encode(prefixed_texts(list(texts), is_query)).tolist()

    return encode


def _lazy_generation_query_encoder():
    """Encode a question, loading the model on first use rather than up front.

    The cold load measured about ten seconds when this was written and 2.7 s on
    2026-09-24 (`docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`).
    Done eagerly in `search()` it was spent before retrieval started, outside the
    optional-stage boundary and outside the caller's deadline, so the first recall
    in a fresh MCP server could burn its whole budget on a signal it had not asked
    for yet and return nothing at all — not even the lexical answer that was ready
    in 1.3 s.

    Resolved here, the same load happens inside the dense leg, which is already
    an abandonable optional stage: the caller gets the lexical answer on time,
    the straggler (a thread `inference_threads` waits for at exit) finishes the
    load, and the next call finds it in the module-level cache. Two stragglers racing the cache would load twice and
    keep the last; the cost is one wasted load, never a wrong vector.

    An unavailable model returns no vector rather than raising, because the
    generation reader already treats an unusable query vector as "no dense
    signal" and that is exactly what an unavailable model means.
    """

    def encode(texts) -> list[list[float]]:
        loaded = _get_embedder()
        if loaded is None:
            return []
        return _generation_embedder(loaded, is_query=True)(texts)

    return encode


class _EmbedderUnavailable(RuntimeError):
    """The model could not be loaded when a chunk first needed a vector."""


def _lazy_passage_encoder():
    """Encode passages, loading the model only when a chunk is not reused.

    A night with no changed page reuses every row and never loads it. See
    `docs/research/2026-09-14-the-model-loads-only-for-new-chunks.md`.
    """

    def encode(texts) -> list[list[float]]:
        loaded = _get_embedder()
        if loaded is None:
            raise _EmbedderUnavailable(str(_embedder_unavailable_reason))
        return _generation_embedder(loaded, is_query=False)(texts)

    return encode


def _resolved_generation_embedder(
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
):
    """Give generation search the encoder it needs when no caller supplied one.

    These three arguments had no default and no caller, so the dense leg of
    generation search never ran in any installed vault: every question was
    answered by token overlap alone, and a question asked in another language
    than the pages could not be answered at all.
    """
    if not semantic:
        return None, None, None
    if embedder is not None:
        return embedder, model_id, model_revision
    return (
        _lazy_generation_query_encoder(),
        EMBEDDING_MODEL,
        EMBEDDING_MODEL_REVISION,
    )


def build_generation_vectors_if_available(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    reuse_from: Path | None = None,
) -> dict[str, object] | None:
    """Build this generation's vectors, or say plainly that they cannot be built.

    Semantic retrieval is the only leg that can answer a question asked in a
    language the pages are not written in, and it stayed unbuilt in every
    installed vault because nothing outside the tests ever called the builder.

    Returning None is a real answer, not a swallowed error: the generation is
    then published with `vector_state: absent`, exactly as before, and the
    doctor reports it. A vault without the optional model keeps working on
    lexical search alone.
    """
    _check_generation_stop(deadline, cancelled)
    if not snapshot.chunks or not _have_encoder_runtime():
        return None
    try:
        artifacts, reused = _built_generation_vectors(
            snapshot,
            generation_directory,
            embedder=_lazy_passage_encoder(),
            model_id=EMBEDDING_MODEL,
            model_revision=EMBEDDING_MODEL_REVISION,
            dimensions=EMBEDDING_DIM,
            reuse_from=reuse_from,
            check_stop=lambda: _check_generation_stop(deadline, cancelled),
        )
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - vectors are optional, a generation is not
        # Losing the whole generation because its optional vectors could not be
        # built would trade a working lexical index for nothing. The generation
        # is published with `vector_state: absent` and the doctor reports it.
        return None
    _check_generation_stop(deadline, cancelled)
    return {
        "artifacts": artifacts,
        "model_id": EMBEDDING_MODEL,
        "model_revision": EMBEDDING_MODEL_REVISION,
        "dimensions": EMBEDDING_DIM,
        "reused_chunks": reused,
        "embedded_chunks": len(snapshot.chunks) - reused,
    }


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
    # Order matters and short-circuit keeps it: `math.isfinite` raises on a
    # non-number, so the type checks must stand in front of it.
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
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


def _resolved_live_scope(
    vault: Path,
    expected_repository_scope: object,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
):
    """This vault's live scope, refusing an expectation of the wrong type."""
    from repository_scope import RepositoryScope, resolve_repository_scope

    if not isinstance(expected_repository_scope, RepositoryScope):
        raise TypeError("expected_repository_scope must be a RepositoryScope")
    return resolve_repository_scope(vault, deadline=deadline, cancelled=cancelled)


def _require_matching_repository(
    vault: Path,
    expected_repository_scope: object | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if expected_repository_scope is None:
        return
    live_scope = _resolved_live_scope(
        vault, expected_repository_scope, deadline=deadline, cancelled=cancelled
    )
    # Identity, not equality: `RepositoryScope` carries `git_commit`, and this
    # vault commits itself, so a four-minute build that ends after a commit was
    # publishing into "a different repository" by that reading. The question
    # here is only whether this is the same checkout — the same one `NEW-65`
    # answered for generation eligibility. Measured 2026-08-24: every rebuild
    # that spanned a commit died at publication with this message.
    if not live_scope.same_repository(expected_repository_scope):
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


def _validation_options(
    remaining: Callable[[], float | None],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, object]:
    options: dict[str, object] = {"coordinator": None}
    if cancelled is not None:
        options["cancelled"] = cancelled
    if deadline is not None:
        options["deadline_seconds"] = remaining()
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
        validate_live_snapshot(
            snapshot, vault, **_validation_options(remaining, deadline, cancelled)
        )
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


def _blank_query(query: str) -> bool:
    return not query or not query.strip()


def _no_stop_requested(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    return deadline is None and cancelled is None


def _check_legacy_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("legacy retrieval cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("legacy retrieval deadline reached")


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


def _entry_has_reparse_point(info: os.stat_result) -> bool:
    """Windows marks a junction or a symlink with a reparse attribute."""
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _is_safe_entry(path: Path, *, directory: bool) -> bool:
    """No symlinks, no reparse points, and the kind the caller expects."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if path.is_symlink() or _entry_has_reparse_point(info):
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


def _classify_entry(
    name: str, parent: Path, directories: list[Path], filenames: list[str]
) -> None:
    path = parent / name
    if name not in SKIP_DIRS and _is_safe_entry(path, directory=True):
        directories.append(path)
        return
    if name.endswith(".md"):
        filenames.append(name)


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
                _classify_entry(entry.name, current_path, directories, filenames)
    except OSError:
        return None
    return directories, filenames


def _is_retired_page(content: str) -> bool:
    """Superseded and archived pages are history, not search results."""
    frontmatter = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not frontmatter:
        return False
    status = re.search(r"^status:\s*(.+?)\s*$", frontmatter.group(1), re.MULTILINE)
    return is_retired(status.group(1)) if status else False


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


def _require_depth_within_limit(depth: int) -> None:
    if depth > MAX_SEARCH_DEPTH:
        raise ValueError("searchable directory depth limit exceeded")


def _require_no_deeper_directories(depth: int, directories: list[str]) -> None:
    if depth >= MAX_SEARCH_DEPTH and directories:
        raise ValueError("searchable directory depth limit exceeded")


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
        _require_depth_within_limit(depth)
        scanned = _scan_directory(current_path, limits)
        if scanned is None:
            continue
        directories, filenames = scanned
        _require_no_deeper_directories(depth, directories)
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
PAGE_TYPE_FIELD_RE = re.compile(
    r"^type:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
)
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


def _deadline_timeout_options(deadline: float | None) -> dict[str, object]:
    if deadline is None:
        return {}
    return {"timeout": max(0.0, min(5.0, deadline - time.monotonic()))}


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
    if _no_stop_requested(deadline, cancelled):
        yield
        return
    stopped = False

    def progress() -> int:
        nonlocal stopped
        stopped = _stop_reached(deadline, cancelled)
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
def _generation_sqlite_guard(
    connection: sqlite3.Connection,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[None]:
    with _sqlite_stop_guard(
        connection,
        deadline,
        cancelled,
        check=_check_generation_stop,
        message="generation SQLite work cancelled or deadline exceeded",
    ):
        yield


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_regular_file(path: Path):
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise PermissionError("generation artifact must be a regular file")
    return before


def _hashed_open_file(
    source,
    before,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[int, str, object, object]:
    """Hash an open file while proving it stays the same file throughout."""
    opened = os.fstat(source.fileno())
    if not os.path.samestat(before, opened):
        raise PermissionError("generation artifact changed while opening")
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(64 * 1024):
        _check_generation_stop(deadline, cancelled)
        size += len(chunk)
        digest.update(chunk)
    _check_generation_stop(deadline, cancelled)
    return size, digest.hexdigest(), opened, os.fstat(source.fileno())


def _require_stable_identity(opened, after_open, after) -> None:
    if (
        not os.path.samestat(opened, after_open)
        or not os.path.samestat(after_open, after)
        or (opened.st_size, opened.st_mtime_ns)
        != (after_open.st_size, after_open.st_mtime_ns)
    ):
        raise PermissionError("generation artifact changed while hashing")


def _require_expected_seal(
    expected: dict[str, object] | None, size: int, checksum: str
) -> None:
    if expected is None:
        return
    if expected.get("size") != size or expected.get("sha256") != checksum:
        raise ValueError("generation artifact does not match active manifest")


# A generation is immutable after activation, so one query seals the same bytes
# four or five times: once when it adopts the generation, and again before and
# after every read. Each seal re-hashes every sealed artifact. Measured on the
# live vault (38 MiB sealed set), that is 0.04-2.06 s per seal and 0.34-2.77 s
# of a 10 s MCP budget, for a verdict that cannot differ between the first call
# and the fifth.
#
# NEW-69 closed the same shape by memoising a verdict keyed by the digests the
# caller had already paid for. That key is not available here: the digest *is*
# the work, so it cannot key its own cache. The key that is free is the one the
# seal tuple already carries — the artifact's stat identity.
#
# A write that keeps dev, ino, mode, size and mtime_ns still moves ctime_ns,
# which no syscall can set: the kernel stamps it on every inode change,
# including the utimensat that would forge mtime. What stat identity cannot
# rule out is a second same-size write landing in the same clock tick as the
# one that was hashed, which would carry the same stamp. Linux stamps inodes
# from the coarse clock, one jiffy wide — 10 ms at the lowest supported
# CONFIG_HZ — so an observation is kept only once the artifact's stamps are
# older than that. This is git's racily-clean rule with the tick made explicit
# instead of inferred from the index's own write time.
_SEAL_STAMP_SETTLE_NS = 20_000_000
_SEAL_OBSERVATION_LIMIT = 32
_seal_observations: dict[tuple, str] = {}
_seal_observation_lock = threading.Lock()


def _stat_identity(status) -> tuple:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _seal_observation_key(
    path: Path, identity: tuple, expected: dict[str, object] | None
) -> tuple:
    """Same file, same bytes on disk, same thing the manifest expected of it."""
    wanted = expected or {}
    return (str(path), identity, wanted.get("size"), wanted.get("sha256"))


def _observed_checksum(key: tuple) -> str | None:
    with _seal_observation_lock:
        return _seal_observations.get(key)


# The cache trusts `st_ctime_ns` to catch a rewrite that forged its mtime: on
# POSIX no syscall can set it, because the kernel stamps it on every inode
# change. Windows has no change-time at all — `st_ctime` there is the creation
# time and survives a rewrite — so the same-size, forged-mtime rewrite would be
# served from the cache unchecked. Measured 2026-08-30: eight Windows jobs, and
# `test_a_rewrite_that_forges_mtime_is_still_caught_by_ctime` read `1 == 2`
# because nothing re-hashed. Where the guard does not exist the cache does not
# either; a seal that can be fooled is worth less than the hashing it saves.
_CTIME_IS_A_CHANGE_TIME = sys.platform != "win32"


def _record_observed_checksum(key: tuple, checksum: str, *, racy: bool) -> None:
    if racy or not _CTIME_IS_A_CHANGE_TIME:
        return
    with _seal_observation_lock:
        if len(_seal_observations) >= _SEAL_OBSERVATION_LIMIT:
            _seal_observations.clear()
        _seal_observations[key] = checksum


def _reset_seal_observations() -> None:
    """Tests that rewrite an artifact in place need the next seal to re-hash."""
    with _seal_observation_lock:
        _seal_observations.clear()


def _hashed_seal(
    path: Path,
    before,
    expected: dict[str, object] | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple:
    began_ns = time.time_ns()
    with path.open("rb") as source:
        size, checksum, opened, after_open = _hashed_open_file(
            source, before, deadline, cancelled
        )
    after = path.lstat()
    _require_stable_identity(opened, after_open, after)
    _require_expected_seal(expected, size, checksum)
    identity = _stat_identity(after)
    _record_observed_checksum(
        _seal_observation_key(path, identity, expected),
        checksum,
        racy=max(after.st_mtime_ns, after.st_ctime_ns)
        > began_ns - _SEAL_STAMP_SETTLE_NS,
    )
    return (*identity, checksum)


def _sealed_file(
    path: Path,
    expected: dict[str, object] | None = None,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple:
    _check_generation_stop(deadline, cancelled)
    before = _require_regular_file(path)
    identity = _stat_identity(before)
    observed = _observed_checksum(_seal_observation_key(path, identity, expected))
    if observed is not None:
        return (*identity, observed)
    return _hashed_seal(
        path, before, expected, deadline=deadline, cancelled=cancelled
    )


def _artifact_descriptors(manifest: Mapping[str, object]) -> dict[str, dict] | None:
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


def _artifact_seals(
    directory: Path,
    descriptors: Mapping[str, dict],
    artifact_names: tuple[str, ...],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[tuple[str, object]] | None:
    seals: list[tuple[str, object]] = []
    for name in artifact_names:
        _check_generation_stop(deadline, cancelled)
        descriptor = descriptors.get(name)
        if not isinstance(descriptor, dict):
            return None
        seals.append(
            (
                name,
                _sealed_file(
                    directory / name, descriptor, deadline=deadline, cancelled=cancelled
                ),
            )
        )
    return seals


def _manifest_seal(
    manifest_path: Path,
    canonical: bytes,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> object | None:
    """A generation may predate the on-disk manifest; absence is not a failure."""
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    return _sealed_file(
        manifest_path,
        {"size": len(canonical), "sha256": hashlib.sha256(canonical).hexdigest()},
        deadline=deadline,
        cancelled=cancelled,
    )


def _generation_consumption_seal(
    catalog: object,
    manifest: dict[str, object],
    artifact_names: tuple[str, ...],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple | None:
    """What this reader saw, so a later check can prove nothing moved."""
    try:
        return _sealed_generation(
            catalog, manifest, artifact_names, deadline=deadline, cancelled=cancelled
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, TypeError, ValueError):
        return None


def _sealed_generation(
    catalog: object,
    manifest: Mapping[str, object],
    artifact_names: tuple[str, ...],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple | None:
    _check_generation_stop(deadline, cancelled)
    generation_id = manifest["generation_id"]
    descriptors = _artifact_descriptors(manifest)
    if not isinstance(generation_id, str) or descriptors is None:
        return None
    directory = Path(getattr(catalog, "generations_path")) / generation_id
    seals = _artifact_seals(
        directory, descriptors, artifact_names, deadline=deadline, cancelled=cancelled
    )
    if seals is None:
        return None
    canonical = _canonical_manifest_bytes(manifest)
    manifest_seal = _manifest_seal(
        directory / "manifest.json", canonical, deadline=deadline, cancelled=cancelled
    )
    return hashlib.sha256(canonical).hexdigest(), manifest_seal, tuple(seals)


def _active_for_repository(
    catalog: object,
    repository_scope: object,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> object:
    if _no_stop_requested(deadline, cancelled):
        return catalog.get_active_for_repository(repository_scope)
    return catalog.get_active_for_repository(
        repository_scope, deadline=deadline, cancelled=cancelled
    )


def _same_sealed_generation(
    catalog: object,
    active: object,
    manifest: dict[str, object],
    artifact_names: tuple[str, ...],
    expected_seal: tuple,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """The active generation is still the one this reader sealed."""
    if not isinstance(active, dict):
        return False
    if _canonical_manifest_bytes(active) != _canonical_manifest_bytes(manifest):
        return False
    seal = _generation_consumption_seal(
        catalog, active, artifact_names, deadline=deadline, cancelled=cancelled
    )
    return seal == expected_seal


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
        active = _active_for_repository(
            catalog,
            RepositoryScope.from_dict(manifest.get("repository_scope")),
            deadline,
            cancelled,
        )
        return _same_sealed_generation(
            catalog,
            active,
            manifest,
            artifact_names,
            expected_seal,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        return False


def _read_only_connect_options(deadline: float | None) -> dict[str, object]:
    return {"uri": True, **_deadline_timeout_options(deadline)}


def _close_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _connectable_generation(
    manifest: dict[str, object], generations_path: object, generation_id: object
) -> bool:
    if not isinstance(generation_id, str) or generations_path is None:
        return False
    return bool(_generation_artifact(manifest, GENERATION_FTS_ARTIFACT))


# Validating the FTS artifact walks every chunk row it holds — 1.95 s on this
# vault — and the verdict is a pure function of those bytes: the rows are checked
# against the artifact's own metadata, and the caller already holds a
# consumption seal proving the bytes match the digest the manifest names. So the
# answer is remembered per set of bytes, for this process, and a different digest
# is a different question. See docs/research/2026-08-24-verify-the-same-bytes-once.md.
_MAX_REMEMBERED_FTS_VERDICTS = 8
_VALID_FTS_ARTIFACTS: OrderedDict[tuple[str, str], bool] = OrderedDict()
_FTS_VERDICT_LOCK = threading.Lock()


def _named_artifact_digest(artifacts: object, name: str) -> str:
    if not isinstance(artifacts, list):
        return ""
    for artifact in artifacts:
        if _artifact_named(artifact, name):
            return str(artifact.get("sha256") or "")
    return ""


def _artifact_named(artifact: object, name: str) -> bool:
    return isinstance(artifact, dict) and artifact.get("path") == name


def _fts_verdict_key(manifest: dict[str, object]) -> tuple[str, str] | None:
    """(generation, digest of search.sqlite3), or None when either is unnamed."""
    generation_id = str(manifest.get("generation_id") or "")
    digest = _named_artifact_digest(
        manifest.get("artifacts"), GENERATION_FTS_ARTIFACT
    )
    if not generation_id or not digest:
        return None
    return generation_id, digest


def _fts_already_valid(key: tuple[str, str] | None) -> bool:
    if key is None:
        return False
    with _FTS_VERDICT_LOCK:
        if key not in _VALID_FTS_ARTIFACTS:
            return False
        _VALID_FTS_ARTIFACTS.move_to_end(key)
        return True


def _remember_valid_fts(key: tuple[str, str] | None) -> None:
    if key is None:
        return
    with _FTS_VERDICT_LOCK:
        _VALID_FTS_ARTIFACTS[key] = True
        while len(_VALID_FTS_ARTIFACTS) > _MAX_REMEMBERED_FTS_VERDICTS:
            _VALID_FTS_ARTIFACTS.popitem(last=False)


def _fts_contents_hold(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    key = _fts_verdict_key(manifest)
    if _fts_already_valid(key):
        return True
    if not _valid_generation_fts(
        connection, manifest, deadline=deadline, cancelled=cancelled
    ):
        return False
    _remember_valid_fts(key)
    return True


def _validated_generation_connection(
    artifact: Path,
    manifest: dict[str, object],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> sqlite3.Connection | None:
    """Open the artifact read-only and hand it back only once it validates."""
    _check_generation_stop(deadline, cancelled)
    connection = sqlite3.connect(
        f"{artifact.resolve().as_uri()}?mode=ro",
        **_read_only_connect_options(deadline),
    )
    try:
        _check_generation_stop(deadline, cancelled)
        if not _fts_contents_hold(connection, manifest, deadline, cancelled):
            connection.close()
            return None
        return connection
    except BaseException:
        _close_quietly(connection)
        raise


def _generation_connection(
    catalog: object,
    manifest: dict[str, object],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> sqlite3.Connection | None:
    _check_generation_stop(deadline, cancelled)
    generations_path = getattr(catalog, "generations_path", None)
    generation_id = manifest.get("generation_id")
    if not _connectable_generation(manifest, generations_path, generation_id):
        return None
    artifact = Path(generations_path) / generation_id / GENERATION_FTS_ARTIFACT
    try:
        return _validated_generation_connection(
            artifact, manifest, deadline, cancelled
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError):
        return None


def _valid_generation_fts(
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    *,
    check_rows: bool = True,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    authoritative_sources: dict[str, dict[str, object]] | None = None,
) -> bool:
    with _generation_sqlite_guard(connection, deadline, cancelled):
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
        _check_generation_stop(deadline, cancelled)
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
    row: tuple, metadata: list, seen: set[str]
) -> None:
    """One more row is allowed only under the ceiling, and only if it is valid."""
    if len(metadata) >= MAX_CORPUS_FILES:
        raise ValueError("generation source row ceiling exceeded")
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
    rows = database.execute(
        "SELECT source_id, relative_path, sha256, size, length(content) FROM source "
        "ORDER BY relative_path, source_id LIMIT ?",
        (MAX_CORPUS_FILES + 1,),
    )
    for row in rows:
        _check_generation_stop(deadline, cancelled)
        _require_admissible_source_row(row, metadata, seen)
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


def _reproducible_by_this_extractor(manifest: Mapping[str, object]) -> bool:
    """Only this extractor's own chunks can be re-derived and compared."""
    import corpus_snapshot

    return manifest.get("extractor_version") == corpus_snapshot.EXTRACTOR_VERSION


_FTS_ROWS_VERDICT = "fts-chunks"


def _fts_rows_digest(manifest: dict[str, object]) -> str | None:
    """The declared digest of the FTS artifact: the identity of its rows."""
    descriptors = _artifact_descriptors(manifest) or {}
    digest = descriptors.get(GENERATION_FTS_ARTIFACT, {}).get("sha256")
    return digest if isinstance(digest, str) else None


def _fts_rows_need_walking(
    state_root: Path, manifest: dict[str, object], deep: bool
) -> tuple[object, str | None, bool]:
    """The verdict cache, the rows' digest, and whether to walk them now."""
    from verified_artifacts import VerifiedArtifacts

    digest = _fts_rows_digest(manifest)
    cache = VerifiedArtifacts(state_root)
    return cache, digest, deep or cache.verdict(_FTS_ROWS_VERDICT, digest) is None


def _remember_walked_rows(cache: object, digest: str | None, walked: bool) -> None:
    """A walk that accepted these bytes is not repeated in the next process."""
    if not walked:
        return
    cache.remember_verdict(_FTS_ROWS_VERDICT, digest)
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
    _check_generation_stop(deadline, cancelled)
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    cache, digest, check_rows = _fts_rows_need_walking(state_root, manifest, deep)
    authoritative_sources = None
    if deep and _reproducible_by_this_extractor(manifest):
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
            check_rows=check_rows,
            deadline=deadline,
            cancelled=cancelled,
            authoritative_sources=authoritative_sources,
        ):
            raise ValueError("generation FTS search artifact is semantically invalid")
    _remember_walked_rows(cache, digest, check_rows)
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


def _valid_fts_schema(connection: sqlite3.Connection) -> bool:
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


def _appended_chunk_rows(rows: list[tuple[object, ...]], chunks) -> bool:
    """False once the stored-chunk ceiling is crossed."""
    for chunk in chunks:
        rows.append(_generation_chunk_row(chunk, len(rows)))
        if len(rows) > MAX_GENERATION_FTS_CHUNKS:
            return False
    return True


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
        except _UnusableAuthoritativeSource:
            return None
        if not _appended_chunk_rows(rows, chunks):
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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_filled_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _fresh_chunk_id(
    chunk_id: object, order: int, chunk_order: object, seen: set[str]
) -> bool:
    return _is_sha256(chunk_id) and chunk_id not in seen and chunk_order == order


def _valid_source_reference(
    source_id: object, source_path: object, parent_page: object
) -> bool:
    if not _is_filled_str(source_path):
        return False
    return source_id == f"source:{source_path}" and parent_page == source_path


def _valid_chunk_identity(row: tuple[object, ...], order: int, seen: set[str]) -> bool:
    chunk_id, chunk_order, source_id, source_path, source_sha256, parent_page = row[:6]
    if not _fresh_chunk_id(chunk_id, order, chunk_order, seen):
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


def _chunk_row_mismatch(
    row: tuple[object, ...],
    order: int,
    expected_chunks: list[tuple[object, ...]] | None,
) -> bool:
    if expected_chunks is None or order >= len(expected_chunks):
        return False
    return row != expected_chunks[order]


def _stored_chunks_match(
    connection: sqlite3.Connection,
    expected_chunks: list[tuple[object, ...]] | None,
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
        return True
    seen: set[str] = set()
    for order, row in enumerate(connection.execute(_FTS_CHUNK_SELECT)):
        _check_generation_stop(deadline, cancelled)
        if not _valid_stored_chunk(row, order, seen):
            return False
        if _chunk_row_mismatch(row, order, expected_chunks):
            return False
        seen.add(str(row[0]))
    return len(seen) == count


_UNUSABLE_CHUNKS = object()


def _valid_fts_metadata(
    connection: sqlite3.Connection, manifest: dict[str, object]
) -> dict | None:
    """The metadata row, or None when the artifact does not describe itself."""
    if not _valid_fts_schema(connection):
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
    rows = _expected_chunk_rows(
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
    count = _stored_chunk_count(connection)
    if not _chunk_count_agrees(count, metadata, expected_chunks):
        return False
    return _stored_chunks_match(
        connection,
        expected_chunks,
        count=count,
        check_rows=check_rows,
        deadline=deadline,
        cancelled=cancelled,
    )


# Words that carry no evidence and, under an implicit AND, single-handedly
# reduce a natural question to no matches at all.
_QUERY_STOPWORDS = frozenset(
    {
        "a", "об", "об", "am", "an", "and", "are", "as", "at", "be", "been",
        "but", "by", "did", "do", "does", "for", "from", "had", "has", "have",
        "how", "i", "in", "is", "it", "many", "me", "much", "my", "of", "on",
        "or", "the", "их", "that", "their", "them", "there", "these", "they",
        "this", "to", "was", "we", "were", "what", "when", "where", "which",
        "who", "why", "will", "with", "you", "your",
        "в", "во", "для", "до", "за", "и", "из", "или", "как", "какой", "когда",
        "мне", "мой", "моя", "на", "не", "о", "от", "по", "при", "с", "у",
        "что", "чтобы", "это", "я",
        "а", "но", "почему", "зачем", "ли", "же", "бы", "то", "так", "где",
        "кто", "чем", "мы", "вы", "ты", "он", "она", "они", "его", "её", "ее",
    }
)


def _carries_evidence(word: str) -> bool:
    return word.casefold() not in _QUERY_STOPWORDS


def _query_terms(query: str) -> list[str]:
    """The words worth matching on, and every word when none of them is.

    A question is mostly function words. Dropping them is what makes an OR
    query rank on evidence rather than on how often "the" occurs.
    """
    words = query.split()
    content = list(filter(_carries_evidence, words))
    return content or words


def _fts_query(query: str) -> str:
    """One FTS5 expression for a natural-language question.

    FTS5 puts an implicit **AND** between bare terms: `MATCH 'one two three'`
    is `one AND two AND three`, and the documentation says so in as many words.
    So a ten-word question only matched a chunk that contained all ten words,
    and questions phrased as questions matched nothing at all.

    Measured on this stand 2026-09-03: "What day of the week do I take a
    cocktail-making class?" retrieved **zero** candidates, while "cocktail
    class" against the same vault retrieved three. Three of fifty questions
    reached the model with an empty evidence manifest for exactly this reason,
    and the model duly said it had nothing to work with.

    Joining with OR is not a loosening of relevance, because bm25 separates a
    query into its component phrases and scores a row by how many it carries:
    a chunk holding every term still outranks one holding a single term. What
    changes is that the one-term chunk is now reachable instead of discarded.
    """
    words = _query_terms(query)
    quoted = [f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words]
    return " OR ".join(quoted)


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
    _add_validity_clauses(clauses, values, as_of=as_of, since=since)
    return (" AND " + " AND ".join(clauses) if clauses else ""), values


def _add_validity_clauses(
    clauses: list[str], values: list[str], *, as_of: str | None, since: str | None
) -> None:
    """Either the as-of window or the current-status rule, then the since floor."""
    if as_of:
        clauses.extend(
            (
                "(valid_from IS NULL OR valid_from = '' OR substr(valid_from, 1, 10) <= ?)",
                "(valid_to IS NULL OR valid_to = '' OR substr(valid_to, 1, 10) > ?)",
            )
        )
        values.extend((as_of[:10], as_of[:10]))
    else:
        clauses.append(current_status_sql())
    if since:
        clauses.append(
            "(valid_from IS NULL OR valid_from = '' OR substr(valid_from, 1, 10) >= ?)"
        )
        values.append(since[:10])


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
    """Without an as-of date a retired page is history, not an answer."""
    return is_retired(row.get("status"))


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
    return (
        not _out_of_scope(row, scope)
        and not _field_mismatch(row, "project", project)
        and not _field_mismatch(row, "authority", authority)
        and not _temporally_excluded(row, since, as_of)
    )


def _subset_or_empty(inner: set[str], outer: set[str]) -> bool:
    """An empty side is never a subset match, however the sets compare."""
    return bool(inner) and inner.issubset(outer)


def _same_project(row_project: str, project: str | None) -> bool:
    if not project:
        return False
    return row_project.casefold() == project.casefold()


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
    """Single hard-filter contract shared by the lexical and NumPy paths."""
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


def _row_text(row: sqlite3.Row, key: str) -> str:
    return row[key] or ""


def _page_title(row: sqlite3.Row) -> str:
    """The page's own title, not the heading of the chunk that matched.

    A row's stored `title` is its chunk's last heading, so a hit in `## Related`
    was reported as a page called "Related" (audit 2026-09-26 C-10). The first
    heading of the ancestry is the page's H1.
    """
    ancestry = json.loads(row["heading_ancestry"] or "[]")
    if ancestry:
        return str(ancestry[0])
    return row["title"] or Path(row["source_path"]).stem


def _first_prose_line(content: str) -> str:
    """The first line under the headings: a summary that does not repeat the title."""
    lines = [line for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return lines[0].strip()[:120] if lines else ""


def _chunk_weight(authority: object, page_type: object, content: object) -> float:
    """Who said it and what the page is, and whether this chunk is prose or a link list."""
    return trust_weight(authority, page_type) * substance_weight(content)


def _generation_result(row: sqlite3.Row, generation_id: str) -> dict[str, object]:
    authority = _row_text(row, "authority")
    content = _row_text(row, "content")
    score = -float(row["rank"]) * _chunk_weight(authority, _row_text(row, "type"), content)
    return {
        "path": row["source_path"],
        "title": _page_title(row),
        "summary": _first_prose_line(content),
        "content": content,
        "score": score,
        "project": _row_text(row, "project"),
        "timestamp": _row_text(row, "valid_from")[:10],
        "chunk_id": row["chunk_id"],
        "candidate_id": row["chunk_id"],
        "source_id": row["source_id"],
        "source_sha256": row["source_sha256"],
        # The turn's own digest: the handle its fact keys are stored under.
        "span_sha256": row["span_sha256"],
        "heading_ancestry": json.loads(row["heading_ancestry"]),
        "type": row["type"],
        "authority": authority,
        "confidence": _row_text(row, "confidence"),
        "status": _row_text(row, "status"),
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
    "valid_to, language, title, content, span_sha256"
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
    """One BM25 over a chunk's title, text and — in a v2 artifact — its fact keys.

    Key expansion, the shape LongMemEval measured as the good one: a turn is found under
    the facts it states as well as under its text, and what the reader gets is still the
    turn. The keys are a column of this table, so they are ranked on the same scale as the
    text. Research: `docs/research/2026-09-17-one-table-one-scale-for-the-keys.md`.
    """
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


def _boosted_generation_score(
    result: dict[str, object], query_words: set[str], project: str | None
) -> float:
    score = float(result["score"])
    if _same_project(str(result["project"]), project):
        score *= 2.0
    title_words = set(str(result["title"]).casefold().split())
    if _subset_or_empty(query_words, title_words):
        score *= 3.0
    return score


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
        result["score"] = _boosted_generation_score(result, query_words, project)


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
    return (
        manifest.get("vector_state") == "complete"
        and manifest.get("embedding_model_id") == model_id
        and manifest.get("embedding_model_revision") == model_revision
        and all(
            _generation_artifact(manifest, name)
            for name in GENERATION_VECTOR_ARTIFACTS
        )
    )


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


def _column(ordered: list[sqlite3.Row], index: int) -> list[object]:
    return [row[index] for row in ordered]


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
        "chunk_ids": _column(ordered, 0),
        "source_ids": _column(ordered, 1),
        "source_paths": _column(ordered, 2),
        "source_sha256": _column(ordered, 3),
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
    """Cosine rows, ordered by the same trust-weighted score the lexical leg uses.

    `_generation_result` already multiplies the lexical rank by `trust_weight`,
    so the lexical leg decides *admission* by who said it and what the page is.
    The dense leg used to overwrite that score with raw cosine, which left the
    vault's own rule -- answer from the compiled pages, read the commentary
    after -- governing only the order of candidates that were already in the
    pool, never which candidates got into it.

    That gap stayed harmless while the commentary was small. Measured on this
    vault on 2026-08-29, `docs/` carried 1,409 chunks against `knowledge/notes`'
    620, and 49 of the 87 research notes had been written in the preceding two
    days. Because the questions are Russian and the decision pages are English,
    same-language commentary took every high cosine: the gold page for eight of
    ten stand questions ranked 117-310 by raw cosine and never entered the
    120-row over-fetch, so no downstream weight could reach it. Weighting here
    puts those same pages at 1-14.
    """
    filters, values = _generation_filters(scope=scope, since=since, as_of=as_of)
    with _generation_sqlite_guard(connection, deadline, cancelled):
        rows = connection.execute(
            f"SELECT {_GENERATION_CHUNK_COLUMNS}, 0.0 AS rank FROM chunks "
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
        # Absent provenance weighs 1.0 by `trust_weight`'s own contract, so a row
        # that carries none is admitted on its cosine alone rather than refused.
        score *= _chunk_weight(result.get("authority"), result.get("type"), result.get("content"))
        result["score"] = round(score, 4)
        result["requested_mode"] = "hybrid"
        result["effective_mode"] = "hybrid"
        results.append(result)
    results.sort(key=lambda item: (-float(item["score"]), str(item["chunk_id"])))
    return results


def _stored_vectors_are_trustworthy(
    metadata: object,
    manifest: dict[str, object],
    ordered: object,
    matrix: object,
    *,
    model_id: str,
    model_revision: str,
    dimensions: object,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """The stored vectors describe this generation and this exact model."""
    return _vector_metadata_matches(
        metadata,
        manifest,
        ordered,
        model_id=model_id,
        model_revision=model_revision,
        dimensions=dimensions,
    ) and _usable_vector_matrix(
        matrix, ordered, dimensions, deadline=deadline, cancelled=cancelled
    )


def _generation_vector_rows(
    query: str,
    connection: sqlite3.Connection,
    manifest: dict[str, object],
    directory: Path,
    generation_id: str,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    scope: str,
    limit: int,
    project: str | None,
    since: str | None,
    as_of: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, object]] | None:
    """The scored rows, or None when the stored vectors cannot be trusted."""
    import numpy as np

    metadata = _read_vector_metadata(directory, deadline, cancelled)
    matrix = np.load(directory / "vectors.npy", mmap_mode="r", allow_pickle=False)
    _check_generation_stop(deadline, cancelled)
    ordered = _ordered_chunk_identity(connection, deadline, cancelled)
    dimensions = manifest.get("vector_dimensions")
    if not _stored_vectors_are_trustworthy(
        metadata,
        manifest,
        ordered,
        matrix,
        model_id=model_id,
        model_revision=model_revision,
        dimensions=dimensions,
        deadline=deadline,
        cancelled=cancelled,
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
        return _generation_vector_rows(
            query,
            connection,
            manifest,
            directory,
            generation_id,
            embedder=embedder,
            model_id=model_id,
            model_revision=model_revision,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = True,
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
    trace_sink: dict[str, object] | None = None,
) -> list[dict]:
    """Public search API — always routes through retrieval.retrieve().

    `trace_sink`, when given, receives the planner trace of the run — which
    generation was selected, which signals ran, what fell back — even when
    the run returns no rows (#26.1).
    """
    limit = _validate_search_limit(limit)
    if _blank_query(query):
        return []
    from retrieval import retrieve_via_search_memory

    generation_embedder, generation_model_id, generation_model_revision = (
        _resolved_generation_embedder(
            semantic, generation_embedder, generation_model_id, generation_model_revision
        )
    )

    return retrieve_via_search_memory(
        query,
        scope=scope,
        limit=limit,
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
        trace_sink=trace_sink,
    )


# The one directory whose file names are identities: pages live flat there, as
# `<slug>.md`, and the legacy telemetry is keyed by that slug.
_FLAT_NOTES_ROOT = "knowledge/notes"


def legacy_candidate_id(path: object) -> str:
    """What names a legacy candidate: the slug of a flat note, else the path without its suffix.

    A stem is an identity only where names are unique. Every
    `knowledge/projects/<slug>/state.md` has the stem `state`, and fusion adds
    ranks per identifier, so two projects' state pages became one candidate and
    the second vanished. Research:
    `docs/research/2026-09-17-two-pages-with-one-file-name-are-two-candidates.md`.
    """
    page = Path(str(path or "").replace("\\", "/"))
    if not page.name:
        return ""
    if page.parent.as_posix() == _FLAT_NOTES_ROOT:
        return page.stem
    return page.with_suffix("").as_posix()


class _PageRead(NamedTuple):
    """What one Markdown page says about itself, read once."""

    relative_path: str
    raw: bytes
    content: str
    title: str
    summary: str
    project: str
    timestamp: str
    authority: str
    page_type: str = ""


def _frontmatter_text(content: str, pattern: re.Pattern[str]) -> str:
    return _extract_frontmatter_field(content, pattern) or ""


def _read_page(page: Path, label: str) -> _PageRead | None:
    try:
        relative_path = page.relative_to(ROOT).as_posix()
        raw = read_stable_bytes(page, MAX_PAGE_BYTES, label=label)
        content = raw.decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None
    title, summary = _extract_title_and_summary(content, page.stem)
    return _PageRead(
        relative_path=relative_path,
        raw=raw,
        content=content,
        title=title,
        summary=summary,
        project=_frontmatter_text(content, PROJECT_FIELD_RE),
        timestamp=_frontmatter_text(content, TIMESTAMP_FIELD_RE)[:10],
        authority=_frontmatter_text(content, AUTHORITY_FIELD_RE),
        page_type=_frontmatter_text(content, PAGE_TYPE_FIELD_RE),
    )


def _page_read_eligible(
    read: _PageRead, *, project: str | None, since: str | None, as_of: str | None
) -> bool:
    return _exact_page_eligible(
        read.relative_path,
        page_project=read.project,
        timestamp=read.timestamp,
        project=project,
        since=since,
        as_of=as_of,
    )


def _page_hit(read: _PageRead, *, score: float, bm25_score: float) -> dict:
    return {
        "path": read.relative_path,
        "title": read.title,
        "summary": read.summary[:120],
        "score": score,
        "bm25_score": bm25_score,
        "project": read.project,
        "timestamp": read.timestamp,
        "candidate_id": legacy_candidate_id(read.relative_path),
        "source_sha256": hashlib.sha256(read.raw).hexdigest(),
        "byte_start": 0,
        "byte_end": len(read.raw),
        "generation": "legacy",
        "authority": read.authority,
    }


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
    read = _read_page(page, "exact filename search page")
    if read is None:
        return None
    if not _page_read_eligible(read, project=project, since=since, as_of=as_of):
        return None
    score = round(10.0 * trust_weight(read.authority, read.page_type), 2)
    return _page_hit(read, score=score, bm25_score=0.0)


def _project_matches(page_project: str, project: str | None) -> bool:
    if not project:
        return True
    return page_project.casefold() == project.casefold()


def _exact_page_eligible(
    relative_path: str,
    *,
    page_project: str,
    timestamp: str,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> bool:
    if not _project_matches(page_project, project):
        return False
    if _before_since(timestamp, since):
        return False
    return not as_of or _valid_as_of(relative_path, as_of)


def _matching_page(pages: list[Path], normalized_stem: str) -> Path | None:
    for item in pages:
        if _normalized_filename_stem(item.name) == normalized_stem:
            return item
    return None


def _retired_page_named(normalized_stem: str) -> Path | None:
    """An archived or superseded page whose filename *is* the query.

    Archiving drops a page from every leg of retrieval, which is right for a
    general question and wrong for one case: asking for the page by name. Until
    2026-09-06 such a question returned nothing at all, and there was no way to
    learn from the system that the page existed.

    Forgotten memories in *Drosophila* persist as silent traces that a reminder
    cue recovers — and the same work shows a permissive cue reconstructs things
    that were never there, which is why this cue is the narrowest available: the
    normalised query must equal the filename stem exactly. Never similarity,
    never a topic, never a title. Deprecated documentation elsewhere is handled
    the same way: kept, labelled, out of default results, reachable when named.
    See `docs/research/2026-09-06-forgotten-not-gone.md`.
    """
    if not normalized_stem:
        return None
    try:
        entries = sorted(KNOWLEDGE_DIR.glob("*.md"))
    except OSError:
        return None
    for candidate in entries[:MAX_SEARCHABLE_PAGES]:
        if _normalized_filename_stem(candidate.name) == normalized_stem:
            return candidate
    return None


def _recalled_retired_hit(
    normalized_stem: str, *, project: str | None, since: str | None, as_of: str | None
) -> dict | None:
    page = _retired_page_named(normalized_stem)
    if page is None:
        return None
    hit = _exact_page_hit(page, project=project, since=since, as_of=as_of)
    if hit is None:
        return None
    hit["retired"] = True
    return hit


def _with_exact_page(
    hits: list[dict],
    pages: list[Path],
    normalized_stem: str,
    *,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict]:
    page = _matching_page(pages, normalized_stem)
    if page is None:
        return _with_recalled_page(
            hits, normalized_stem, project=project, since=since, as_of=as_of
        )
    relative = page.relative_to(ROOT).as_posix()
    if any(hit["path"] == relative for hit in hits):
        return hits
    extra = _exact_page_hit(page, project=project, since=since, as_of=as_of)
    return hits if extra is None else [*hits, extra]


def _with_recalled_page(
    hits: list[dict],
    normalized_stem: str,
    *,
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> list[dict]:
    """Nothing active bears this name; an archived page of that name may."""
    recalled = _recalled_retired_hit(
        normalized_stem, project=project, since=since, as_of=as_of
    )
    return hits if recalled is None else [*hits, recalled]


def _filename_matches(hits: list[dict], normalized_stem: str) -> list[dict]:
    return [
        hit
        for hit in hits
        if _normalized_filename_stem(str(hit["path"])) == normalized_stem
    ]


def _promoted_filename_first(hits: list[dict], normalized_stem: str) -> list[dict]:
    """Keep a pure ranked list, but a filename match leads it."""
    matches = _filename_matches(hits, normalized_stem)
    if not matches:
        return hits
    matches.sort(key=lambda hit: (_notes_first(hit), -hit["score"], hit["path"]))
    best = matches[0]
    return [best, *(hit for hit in hits if hit["path"] != best["path"])]


def _document_terms(page: Path, title: str, summary: str, body: str) -> set[str]:
    haystack = f"{page.stem.replace('-', ' ')} {title} {summary} {body}".casefold()
    return set(re.findall(r"\w+", haystack))


def _direct_match_score(
    page: Path, title: str, read: _PageRead, query_terms: set[str]
) -> float:
    """Literal matching has no BM25, so the term count carries the base score."""
    score = float(len(query_terms))
    if query_terms.issubset(set(re.findall(r"\w+", title.casefold()))):
        score *= 3.0
    if query_terms.issubset(set(re.findall(r"\w+", page.stem.casefold()))):
        score *= 4.0
    return score * trust_weight(read.authority, read.page_type)


def _evidence_terms(query: str) -> set[str]:
    """The question's words without stop words, or all of them when that is all it has."""
    words = re.findall(r"\w+", query.casefold())
    return {word for word in words if _carries_evidence(word)} or set(words)


def _direct_page_hit(
    page: Path,
    *,
    query_terms: set[str],
    project: str | None,
    since: str | None,
    as_of: str | None,
) -> dict | None:
    """One literal Markdown match, or None when the page does not qualify."""
    read = _read_page(page, "search page")
    if read is None:
        return None
    body = _strip_frontmatter(read.content)
    shared = query_terms & _document_terms(page, read.title, read.summary, body)
    if not shared or not _page_read_eligible(
        read, project=project, since=since, as_of=as_of
    ):
        return None
    score = round(_direct_match_score(page, read.title, read, shared), 2)
    return {
        **_page_hit(read, score=score, bm25_score=score),
        "fallback_reason": "no_active_generation",
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
    """Return bounded literal matches from authoritative Markdown only.

    A page qualifies on any word of the question that carries evidence and ranks
    by how many it shares; requiring every word, stop words included, found
    nothing for a natural question (audit B-22,
    docs/research/2026-09-25-a-question-finds-pages-without-a-generation.md).
    """
    query_terms = _evidence_terms(query)
    if not query_terms:
        return []
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
    return results[: max(limit * 3, limit)]


def markdown_hits(
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
) -> list[dict]:
    """The lexical signal when no generation is active: Markdown, read directly.

    Every hit says `fallback_reason: no_active_generation`, so the answer is
    reported partial; a page whose filename is the query is kept in front, and
    an archived page of that name is recalled, labelled `retired`. Since
    2026-09-23 this is the only fallback: the legacy FTS5 index is gone. See
    `docs/research/2026-09-23-the-generation-is-the-only-index.md`.
    """
    _check_legacy_stop(deadline, cancelled)
    if _blank_query(query):
        return []
    pages = _resolved_pages(page_paths, scope, deadline)
    hits = _direct_markdown_hits(
        query,
        pages,
        limit=limit,
        project=project,
        since=since,
        as_of=as_of,
        deadline=deadline,
        cancelled=cancelled,
    )
    normalized_stem = _normalized_filename_stem(query)
    hits = _with_exact_page(hits, pages, normalized_stem, project=project, since=since, as_of=as_of)
    return _promoted_filename_first(hits, normalized_stem)


def _resolved_pages(
    page_paths: list[Path] | None, scope: str, deadline: float | None
) -> list[Path]:
    if page_paths is not None:
        return page_paths
    return _collect_pages(scope, deadline=deadline or float("inf"))


def _notes_first(row: dict) -> int:
    """Duplicates prefer the canonical notes tree."""
    return 0 if "knowledge/notes/" in row["path"] else 1


_PAGE_STATUS_RE = re.compile(r"^status:\s*[\"\']?([^\"\'\n]+)[\"\']?\s*$", re.MULTILINE)


def main() -> int:
    p = argparse.ArgumentParser(description="Hybrid search over the vault's knowledge.")
    p.add_argument("query", nargs="?", default=None, help="Search query")
    p.add_argument("--scope", choices=["all", "wiki", "memory", "knowledge"], default="all")
    p.add_argument("--limit", type=_cli_search_limit, default=10)
    p.add_argument("--project", default=None, help="Boost results from this project slug")
    p.add_argument("--since", default=None, help="Only results since YYYY-MM-DD")
    p.add_argument("--as-of", dest="as_of", default=None, help="Only results valid on YYYY-MM-DD")
    # On by default: measured on this vault, "почему systemd таймер, а не cron"
    # returns nothing without it and four results with it. The dense leg costs
    # about three seconds once the model is warm, and it fails soft — no model or
    # no vectors in the active generation simply means the lexical answer.
    p.add_argument(
        "--semantic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Vector search over the active generation (default: on)",
    )
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
    # The cross-encoder costs about twenty seconds to load in a fresh process
    # and buys `hit@1` 0.0 → 0.1 and `hit@5` 0.6 → 0.7 on this vault's stand
    # (measured 2026-08-24). A resident MCP server pays that once and keeps it;
    # a one-shot CLI call pays it every time, which is why the CLI leaves it off
    # unless asked. Measured 2026-08-25: 30.3 s with it, 10.7 s without.
    p.add_argument(
        "--rerank",
        action="store_true",
        help="Load the cross-encoder reranker (about 20s in a cold process)",
    )
    p.add_argument(
        "--no-rerank",
        action="store_true",
        help="Kept for compatibility: the CLI already skips the reranker",
    )
    p.add_argument(
        "--rebuild", action="store_true", help="Rebuild the evidence generation the search reads"
    )
    p.add_argument("--status", action="store_true", help="Show what the search reads")
    p.add_argument("--stdin", action="store_true", help="Read query from stdin (injection-safe)")
    args = p.parse_args()

    if args.stdin:
        args.query = sys.stdin.read().strip()
    return _run_cli_command(args)


def _run_cli_command(args: argparse.Namespace) -> int:
    """The one mode the arguments asked for, in the order they take priority."""
    modes = (
        (args.status, _print_status),
        (args.rebuild, _rebuild_generation_cli),
        (not args.query, _print_cli_usage),
    )
    for chosen, run in modes:
        if chosen:
            return run()
    return _run_cli_search(args)


def _print_cli_usage() -> int:
    print("Usage: python search_memory.py \"<query>\"", file=sys.stderr)
    return 1


def _active_generation_line() -> str:
    """What the search reads: the active generation, else Markdown directly."""
    catalog = _active_generation_catalog()
    manifest = None if catalog is None else catalog.get_active()
    if not isinstance(manifest, dict):
        return "Active generation: none (search reads Markdown directly; run --rebuild)"
    return (
        f"Active generation: {manifest.get('generation_id')} "
        f"({manifest.get('extractor_version')}, vectors {manifest.get('vector_state')}, "
        f"model {manifest.get('embedding_model_id')})"
    )


def _print_status() -> int:
    """The generation the search reads and the pages it would index."""
    print(_active_generation_line())
    print(f"Searchable pages (superseded excluded): {len(_collect_pages('all'))}")
    return 0


def _rebuild_generation_cli() -> int:
    """Rebuild the evidence generation under the maintenance fence: the one index."""
    import doctor

    result = doctor.run_generation_maintenance(ROOT, STATE_ROOT, force_rebuild=True)
    status = str(result.get("status"))
    print(f"Generation rebuild: {status} (id={result.get('generation_id') or 'none'})")
    if result.get("reason"):
        print(f"  reason: {result['reason']}")
    return 0 if status in {"built", "current"} else 1


def _run_cli_search(args: argparse.Namespace) -> int:
    t0 = time.time()
    results = search(
        args.query, args.scope, args.limit,
        project=args.project,
        since=args.since,
        as_of=args.as_of,
        semantic=args.semantic,
        profile=args.profile,
        graph=not args.no_graph,
        rerank=args.rerank and not args.no_rerank,
    )
    elapsed = time.time() - t0

    if not results:
        print(f"No results for '{args.query}' ({elapsed:.3f}s)")
        return 0

    _print_search_results(args.query, results, elapsed)
    return 0


def _print_search_results(query: str, results: list[dict], elapsed: float) -> None:
    print(f"Found {len(results)} result(s) for '{query}' ({elapsed:.3f}s):\n")
    for i, r in enumerate(results, 1):
        proj_tag = f" [{r['project']}]" if r["project"] else ""
        ts_tag = f" ({r['timestamp']})" if r["timestamp"] else ""
        print(f"{i}. [{r['score']}] {r['title']}{proj_tag}{ts_tag}")
        print(f"   {r['path']}")
        snippet = result_snippet(r)
        if snippet:
            print(f"   {snippet}")
        print()


SNIPPET_CHARS = 240


def result_snippet(result: dict) -> str:
    """The first words of the hit's text after its heading; the summary otherwise.

    A chunk's summary is its heading, and a heading such as "Consequences" says
    nothing on its own line. See
    `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.
    """
    text = _body_text(str(result.get("content") or ""))
    if not text:
        return str(result.get("summary") or "")
    return _clipped(text, SNIPPET_CHARS)


def _body_text(content: str) -> str:
    lines = [line.strip() for line in content.splitlines()]
    return " ".join(line for line in lines if _is_body_line(line))


def _is_body_line(line: str) -> bool:
    return bool(line) and not line.startswith("#")


def _clipped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


if __name__ == "__main__":
    raise SystemExit(main())
