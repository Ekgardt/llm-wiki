"""LanceDB embedded vector backend — IVF_PQ compatibility path, no daemon.

Replaces numpy brute-force for vector search when --extra hybrid installed.
LanceDB stores vectors in Lance columnar format (memory-mapped, fast load).
Embedded — no server process, no daemon, no network.

Architecture:
  SQLite FTS5 (BM25) + LanceDB (vector IVF_PQ when indexed) = hybrid search.
  Exact NumPy remains the default generation backend; Lance is compatibility.

Install: uv sync --extra hybrid
Storage: cache/lancedb/ (gitignored, rebuildable from Markdown)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import STATE_ROOT  # noqa: E402

LANCEDB_DIR = STATE_ROOT / "cache" / "lancedb"
TABLE_NAME = "pages_vec"
STAGING_TABLE_NAME = "pages_vec_staging"
EMBEDDING_DIM = 384

_lancedb: object | None = None  # lazy connection
_PROJECT_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def distance_to_similarity(distance: float) -> float:
    """Convert Lance smaller-is-better distance to larger-is-better similarity."""
    if distance < 0:
        distance = 0.0
    return 1.0 / (1.0 + float(distance))


def _rows_from_lance_hits(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Lance hits: keep lance_distance raw; score is similarity."""
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        distance = float(item.get("_distance", item.get("lance_distance", 1.0)))
        similarity = round(distance_to_similarity(distance), 6)
        rows.append(
            {
                "path": item.get("path", ""),
                "title": item.get("title", ""),
                "summary": (item.get("summary") or "")[:120],
                "project": item.get("project", "") or "",
                "timestamp": item.get("timestamp", "") or "",
                "status": item.get("status", "") or "",
                "valid_from": item.get("valid_from", "") or "",
                "valid_to": item.get("valid_to", "") or "",
                "authority": item.get("authority", "") or "",
                "lance_distance": round(distance, 6),
                "vector_score": similarity,
                "score": similarity,
            }
        )
    rows.sort(key=lambda row: (-float(row["vector_score"]), str(row["path"])))
    return rows


def apply_vector_filters(
    rows: list[dict[str, Any]],
    *,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Apply the same hard filters used by the exact NumPy generation path."""
    filtered: list[dict[str, Any]] = []
    for row in rows:
        proj = str(row.get("project") or "")
        ts = str(row.get("timestamp") or row.get("valid_from") or "")
        status = str(row.get("status") or "").casefold()
        valid_from = str(row.get("valid_from") or "")[:10]
        valid_to = str(row.get("valid_to") or "")[:10]

        if project and proj.casefold() != project.casefold():
            continue
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


def upsert_vectors(
    paths: list[str],
    titles: list[str],
    summaries: list[str],
    projects: list[str],
    timestamps: list[str],
    vectors: list[list[float]],
    model: str = "BAAI/bge-small-en-v1.5",
) -> int:
    """Build-validate-activate vector table. Returns count of vectors stored."""
    db = _get_db()
    if db is None:
        return 0

    try:
        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("path", pa.string()),
                pa.field("title", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("project", pa.string()),
                pa.field("timestamp", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
                pa.field("model", pa.string()),
            ]
        )
        data = pa.table(
            {
                "path": paths,
                "title": titles,
                "summary": summaries,
                "project": projects,
                "timestamp": timestamps,
                "vector": vectors,
                "model": [model] * len(vectors),
            },
            schema=schema,
        )

        # Drop any leftover staging table, build into staging, validate, activate.
        try:
            db.drop_table(STAGING_TABLE_NAME)
        except Exception:
            pass
        table = db.create_table(STAGING_TABLE_NAME, data=data)
        try:
            table.create_index(
                index_type="IVF_PQ",
                vector_column_name="vector",
                num_partitions=min(256, max(1, len(vectors) // 100)),
            )
        except Exception:
            pass

        count = table.count_rows()
        if count != len(vectors):
            try:
                db.drop_table(STAGING_TABLE_NAME)
            except Exception:
                pass
            return 0

        # Legacy mutable path remains compatibility-only. Prefer
        # publish_generation_vectors() which never drops the live table.
        try:
            db.drop_table(TABLE_NAME)
        except Exception:
            pass
        live = db.create_table(TABLE_NAME, data=data)
        try:
            live.create_index(
                index_type="IVF_PQ",
                vector_column_name="vector",
                num_partitions=min(256, max(1, len(vectors) // 100)),
            )
        except Exception:
            pass
        try:
            db.drop_table(STAGING_TABLE_NAME)
        except Exception:
            pass
        return len(vectors)
    except Exception:
        try:
            db = _get_db()
            if db is not None:
                db.drop_table(STAGING_TABLE_NAME)
        except Exception:
            pass
        return 0


def publish_generation_vectors(
    *,
    generation_dir: Path,
    paths: list[str],
    titles: list[str],
    summaries: list[str],
    projects: list[str],
    timestamps: list[str],
    vectors: list[list[float]],
    model: str = "BAAI/bge-small-en-v1.5",
) -> dict[str, object]:
    """Write Lance vectors under an immutable generation directory only.

    Never drops or mutates the legacy live TABLE_NAME. Activation is owned by
    the generation catalog CAS outside this helper.
    """
    directory = Path(generation_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not _have_lancedb():
            return {"status": "skipped", "reason": "lancedb_unavailable", "count": 0}
        import lancedb
        import pyarrow as pa

        lance_dir = directory / "lance"
        if lance_dir.exists() or lance_dir.is_symlink():
            return {"status": "skipped", "reason": "already_exists", "count": 0}
        db = lancedb.connect(str(lance_dir))
        schema = pa.schema(
            [
                pa.field("path", pa.string()),
                pa.field("title", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("project", pa.string()),
                pa.field("timestamp", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
                pa.field("model", pa.string()),
            ]
        )
        data = pa.table(
            {
                "path": paths,
                "title": titles,
                "summary": summaries,
                "project": projects,
                "timestamp": timestamps,
                "vector": vectors,
                "model": [model] * len(vectors),
            },
            schema=schema,
        )
        table = db.create_table("chunks", data=data)
        if table.count_rows() != len(vectors):
            return {"status": "failed", "reason": "count_mismatch", "count": 0}
        return {"status": "ok", "reason": None, "count": len(vectors), "path": str(lance_dir)}
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}", "count": 0}


def vector_search(
    query_vec: list[float],
    limit: int = 20,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """Search vectors via LanceDB. Returns ranked larger-is-better similarities."""
    db = _get_db()
    if db is None:
        return []

    try:
        table = db.open_table(TABLE_NAME)
        query = table.search(query_vec).limit(max(limit * 3, limit))

        if project:
            if not _PROJECT_SAFE.match(project):
                return []
            query = query.where(f"project = '{project}'")

        raw = query.to_list()
        rows = _rows_from_lance_hits(raw)
        rows = apply_vector_filters(rows, project=None, since=since, as_of=as_of)
        # project already applied in Lance where possible; re-apply for parity.
        if project:
            rows = apply_vector_filters(rows, project=project, since=None, as_of=None)
        return rows[:limit]
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
