"""LanceDB embedded vector backend — HNSW + hybrid, no daemon.

Replaces numpy brute-force for vector search when --extra hybrid installed.
LanceDB stores vectors in Lance columnar format (memory-mapped, fast load).
Embedded — no server process, no daemon, no network.

Architecture:
  SQLite FTS5 (BM25) + LanceDB (vector HNSW) = hybrid search, all embedded.

Install: uv sync --extra hybrid
Storage: cache/lancedb/ (gitignored, rebuildable from Markdown)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import STATE_ROOT  # noqa: E402

LANCEDB_DIR = STATE_ROOT / "cache" / "lancedb"
TABLE_NAME = "pages_vec"
EMBEDDING_DIM = 384

_lancedb: object | None = None  # lazy connection


def _have_lancedb() -> bool:
    """Check if lancedb is importable."""
    try:
        import lancedb  # noqa: F401
        return True
    except ImportError:
        return False


def _get_db():
    """Get or create LanceDB connection. Returns None if unavailable."""
    global _lancedb
    if _lancedb is not None:
        return _lancedb
    if not _have_lancedb():
        return None
    try:
        import lancedb
        LANCEDB_DIR.mkdir(parents=True, exist_ok=True)
        _lancedb = lancedb.connect(str(LANCEDB_DIR))
        return _lancedb
    except Exception:
        return None


def have_lancedb() -> bool:
    """Quick probe: is LanceDB available and has data?"""
    db = _get_db()
    if db is None:
        return False
    try:
        tables = db.table_names()
        return TABLE_NAME in tables
    except Exception:
        return False


def upsert_vectors(
    paths: list[str],
    titles: list[str],
    summaries: list[str],
    projects: list[str],
    timestamps: list[str],
    vectors: list[list[float]],
    model: str = "BAAI/bge-small-en-v1.5",
) -> int:
    """Create or replace the vector table. Returns count of vectors stored."""
    db = _get_db()
    if db is None:
        return 0

    try:
        import pyarrow as pa

        # Drop existing table if any (full rebuild).
        try:
            db.drop_table(TABLE_NAME)
        except Exception:
            pass

        # Create table with Arrow schema for type safety.
        schema = pa.schema([
            pa.field("path", pa.string()),
            pa.field("title", pa.string()),
            pa.field("summary", pa.string()),
            pa.field("project", pa.string()),
            pa.field("timestamp", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
            pa.field("model", pa.string()),
        ])

        data = pa.table({
            "path": paths,
            "title": titles,
            "summary": summaries,
            "project": projects,
            "timestamp": timestamps,
            "vector": vectors,
            "model": [model] * len(vectors),
        }, schema=schema)

        table = db.create_table(TABLE_NAME, data=data)

        # Create vector index for HNSW search.
        try:
            table.create_index(
                index_type="IVF_PQ",
                vector_column_name="vector",
                num_partitions=min(256, max(1, len(vectors) // 100)),
            )
        except Exception:
            pass  # Index creation is best-effort; search works without it.

        return len(vectors)
    except Exception:
        return 0


def vector_search(
    query_vec: list[float],
    limit: int = 20,
    project: str | None = None,
) -> list[dict]:
    """Search vectors using LanceDB HNSW. Returns ranked results.

    Falls back to None (caller uses numpy brute-force) if unavailable.
    """
    db = _get_db()
    if db is None:
        return []

    try:
        table = db.open_table(TABLE_NAME)
        query = table.search(query_vec).limit(limit)

        # Apply project filter if specified.
        if project:
            query = query.where(f"project = '{project.lower()}'")

        results = query.to_list()

        return [
            {
                "path": r.get("path", ""),
                "title": r.get("title", ""),
                "summary": (r.get("summary") or "")[:120],
                "score": round(r.get("_distance", 1.0), 4),
                "project": r.get("project", ""),
                "timestamp": r.get("timestamp", ""),
            }
            for r in results
        ]
    except Exception:
        return []


def vector_count() -> int:
    """Return number of vectors in LanceDB, or 0 if unavailable."""
    if not have_lancedb():
        return 0
    try:
        db = _get_db()
        table = db.open_table(TABLE_NAME)
        return table.count_rows()
    except Exception:
        return 0


if __name__ == "__main__":
    if _have_lancedb():
        count = vector_count()
        print(f"LanceDB: {count} vectors in {LANCEDB_DIR}")
    else:
        print("LanceDB not installed. Run: uv sync --extra hybrid")
