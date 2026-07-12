"""PostgreSQL + pgvector backend for hybrid knowledge search.

This module provides a PostgreSQL-based search backend that performs
BM25 + vector + graph-neighbor fusion in a single SQL query (CTE with
weighted RRF). It is the recommended backend for production use.

When PostgreSQL is not available (not installed, not running, or
pgvector extension missing), all functions degrade gracefully and the
caller falls back to the SQLite/FTS5 backend in search_memory.py.

Connection management uses NullConnectionPool — ideal for short-lived
CLI script invocations (no idle connections held between runs).

Install: uv sync --extra postgres
Setup:   python scripts/pg_init.py
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Lazy imports — module is importable without psycopg installed.
_psycopg: Any = None
_register_vector: Any = None

DEFAULT_DSN = os.environ.get("LLMWIKI_PG_DSN", "host=127.0.0.1 port=5433 dbname=llmwiki user=postgres")

_pool: Any = None  # NullConnectionPool instance (lazy)

SCHEMA_VERSION = 1

SCHEMA_DDL = """\
-- llm-wiki PostgreSQL schema — v4.0 hybrid search
-- Requires: CREATE EXTENSION vector;  (pgvector >= 0.8.0)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS pages (
    id            bigserial PRIMARY KEY,
    slug          text        NOT NULL UNIQUE,
    title         text        NOT NULL DEFAULT '',
    summary       text        NOT NULL DEFAULT '',
    body          text        NOT NULL DEFAULT '',
    fts           tsvector    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,   '')), 'A') ||
        setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(body,    '')), 'D')
    ) STORED,
    frontmatter   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    project       text,
    type          text        NOT NULL DEFAULT 'concept',
    status        text        NOT NULL DEFAULT 'active',
    confidence    text,
    source_authority text,
    timestamp     timestamptz NOT NULL DEFAULT now(),
    valid_from    timestamptz NOT NULL DEFAULT now(),
    valid_to      timestamptz,
    content_hash  text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pages_fts_gin        ON pages USING GIN (fts);
CREATE INDEX IF NOT EXISTS pages_project_idx    ON pages (project);
CREATE INDEX IF NOT EXISTS pages_type_idx       ON pages (type);
CREATE INDEX IF NOT EXISTS pages_status_idx     ON pages (status);
CREATE INDEX IF NOT EXISTS pages_timestamp_idx  ON pages (timestamp DESC);
CREATE INDEX IF NOT EXISTS pages_slug_idx       ON pages (slug);

-- Partial index: only 'active' pages are searched by default.
CREATE INDEX IF NOT EXISTS pages_active_fts
    ON pages USING GIN (fts) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS pages_vec (
    page_id     bigint PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    embedding   vector(384) NOT NULL,
    model       text NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pages_vec_hnsw
    ON pages_vec USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS edges (
    id          bigserial PRIMARY KEY,
    src_id      bigint NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    dst_slug    text   NOT NULL,
    dst_id      bigint REFERENCES pages(id) ON DELETE CASCADE,
    edge_type   text   NOT NULL,
    valid_from  timestamptz NOT NULL DEFAULT now(),
    valid_to    timestamptz,
    weight      real NOT NULL DEFAULT 1.0,
    UNIQUE (src_id, dst_slug, edge_type)
);

CREATE INDEX IF NOT EXISTS edges_src_idx  ON edges (src_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS edges_dst_idx  ON edges (dst_id) WHERE valid_to IS NULL AND dst_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS edges_slug_idx ON edges (dst_slug);
CREATE INDEX IF NOT EXISTS edges_type_idx ON edges (edge_type);

CREATE TABLE IF NOT EXISTS access_log (
    id          bigserial PRIMARY KEY,
    page_id     bigint NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    accessed_at timestamptz NOT NULL DEFAULT now(),
    source      text NOT NULL,
    query       text,
    rank        integer,
    clicked     boolean DEFAULT false
);

CREATE INDEX IF NOT EXISTS access_log_page_time ON access_log (page_id, accessed_at DESC);
CREATE INDEX IF NOT EXISTS access_time_idx      ON access_log (accessed_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO schema_meta (key, value) VALUES ('version', %s)
ON CONFLICT (key) DO NOTHING;
"""

HYBRID_SEARCH_SQL = """\
WITH semantic AS (
    SELECT v.page_id AS id,
           RANK () OVER (ORDER BY v.embedding <=> %(embedding)s) AS rank
    FROM pages_vec v
    JOIN pages p ON p.id = v.page_id
    WHERE (%(project)s IS NULL OR p.project = %(project)s)
      AND p.status = 'active'
      AND (%(as_of)s IS NULL
           OR (p.valid_from <= %(as_of)s
               AND (p.valid_to IS NULL OR p.valid_to > %(as_of)s)))
    ORDER BY v.embedding <=> %(embedding)s
    LIMIT 20
),
keyword AS (
    SELECT p.id AS id,
           RANK () OVER (
               ORDER BY ts_rank_cd(p.fts, query) DESC
           ) AS rank
    FROM pages p,
         plainto_tsquery('english', %(query)s) query
    WHERE p.fts @@ query
      AND (%(project)s IS NULL OR p.project = %(project)s)
      AND p.status = 'active'
      AND (%(as_of)s IS NULL
           OR (p.valid_from <= %(as_of)s
               AND (p.valid_to IS NULL OR p.valid_to > %(as_of)s)))
    ORDER BY ts_rank_cd(p.fts, query) DESC
    LIMIT 20
),
graph AS (
    SELECT e.dst_id AS id,
           RANK () OVER (ORDER BY count(*) DESC) AS rank,
           count(*)::real AS graph_boost
    FROM keyword k
    JOIN edges e ON e.src_id = k.id
    WHERE e.edge_type = 'wikilink'
      AND e.valid_to IS NULL
      AND e.dst_id IS NOT NULL
    GROUP BY e.dst_id
    LIMIT 20
)
SELECT
    p.id,
    p.slug,
    p.title,
    p.summary,
    p.project,
    p.type,
    p.timestamp,
    ( 2.0 * COALESCE(1.0 / (%(k_rrf)s + keyword.rank), 0.0)
    + 1.0 * COALESCE(1.0 / (%(k_rrf)s + semantic.rank), 0.0)
    + 0.5 * COALESCE(
          graph.graph_boost * 1.0 / (%(k_rrf)s * 2 + COALESCE(graph.rank, %(k_rrf)s)),
          0.0
      )
    ) AS score
FROM pages p
LEFT JOIN keyword  ON keyword.id  = p.id
LEFT JOIN semantic ON semantic.id = p.id
LEFT JOIN graph    ON graph.id    = p.id
WHERE keyword.id IS NOT NULL OR semantic.id IS NOT NULL OR graph.id IS NOT NULL
ORDER BY score DESC
LIMIT %(limit)s;
"""

BM25_ONLY_SQL = """\
SELECT p.id, p.slug, p.title, p.summary, p.project, p.type, p.timestamp,
       ts_rank_cd(p.fts, query) AS score
FROM pages p,
     plainto_tsquery('english', %(query)s) query
WHERE p.fts @@ query
  AND p.status = 'active'
  AND (%(project)s IS NULL OR p.project = %(project)s)
  AND (%(as_of)s IS NULL
       OR (p.valid_from <= %(as_of)s
           AND (p.valid_to IS NULL OR p.valid_to > %(as_of)s)))
ORDER BY ts_rank_cd(p.fts, query) DESC
LIMIT %(limit)s;
"""


def _ensure_psycopg() -> bool:
    """Lazy-import psycopg. Returns True if available."""
    global _psycopg, _register_vector
    if _psycopg is not None:
        return True
    try:
        import psycopg
        _psycopg = psycopg
        try:
            from pgvector.psycopg import register_vector
            _register_vector = register_vector
        except ImportError:
            pass
        return True
    except ImportError:
        return False


def _dsn() -> str:
    return os.environ.get("LLMWIKI_PG_DSN", DEFAULT_DSN)


def _configure_conn(conn: Any) -> None:
    """Called once per new connection. Registers pgvector type adapter."""
    if _register_vector is not None:
        try:
            _register_vector(conn)
        except Exception:
            pass


def get_pool() -> Any:
    """Lazy NullConnectionPool. Opens on first use, closes on process exit."""
    global _pool
    if not _ensure_psycopg():
        return None
    if _pool is not None and not getattr(_pool, "closed", True):
        return _pool
    from psycopg_pool import NullConnectionPool
    _pool = NullConnectionPool(
        conninfo=_dsn(),
        open=False,
        timeout=5,
        configure=_configure_conn,
    )
    _pool.open(wait=True)
    return _pool


@contextmanager
def db() -> Iterator[Any]:
    """Borrow a connection for one operation. COMMIT on exit, ROLLBACK on error."""
    pool = get_pool()
    if pool is None:
        raise RuntimeError("psycopg not installed — cannot use PostgreSQL backend")
    with pool.connection() as conn:
        yield conn


def pg_available() -> bool:
    """Quick probe: is PostgreSQL + pgvector reachable? Never raises."""
    if not _ensure_psycopg():
        return False
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            return row is not None
    except Exception:
        return False


def init_schema(dsn: str | None = None) -> bool:
    """Create schema (idempotent). Returns True on success, False on failure."""
    if not _ensure_psycopg():
        return False
    target_dsn = dsn or _dsn()
    try:
        with _psycopg.connect(target_dsn, autocommit=True) as conn:
            _configure_conn(conn)
            conn.execute(SCHEMA_DDL, (str(SCHEMA_VERSION),))
        return True
    except Exception:
        return False


def upsert_page(
    conn: Any,
    slug: str,
    title: str,
    summary: str,
    body: str,
    frontmatter: dict | None = None,
    project: str | None = None,
    page_type: str = "concept",
    status: str = "active",
    confidence: str | None = None,
    source_authority: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    content_hash: str | None = None,
    timestamp: str | None = None,
) -> int:
    """Insert or update a page. Returns the page ID."""
    import json
    fm = json.dumps(frontmatter or {})
    row = conn.execute(
        """
        INSERT INTO pages (slug, title, summary, body, frontmatter, project,
                           type, status, confidence, source_authority,
                           valid_from, valid_to, content_hash, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (slug) DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            body = EXCLUDED.body,
            frontmatter = EXCLUDED.frontmatter,
            project = EXCLUDED.project,
            type = EXCLUDED.type,
            status = EXCLUDED.status,
            confidence = EXCLUDED.confidence,
            source_authority = EXCLUDED.source_authority,
            valid_from = EXCLUDED.valid_from,
            valid_to = EXCLUDED.valid_to,
            content_hash = EXCLUDED.content_hash,
            timestamp = EXCLUDED.timestamp,
            updated_at = now()
        RETURNING id
        """,
        (slug, title, summary, body, fm, project, page_type, status,
         confidence, source_authority, valid_from, valid_to, content_hash, timestamp),
    ).fetchone()
    return row[0] if row else 0


def upsert_embedding(conn: Any, page_id: int, embedding: list[float], model: str) -> None:
    """Insert or update a vector embedding for a page."""
    conn.execute(
        """
        INSERT INTO pages_vec (page_id, embedding, model)
        VALUES (%s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            embedding = EXCLUDED.embedding,
            model = EXCLUDED.model,
            embedded_at = now()
        """,
        (page_id, embedding, model),
    )


def upsert_edge(
    conn: Any,
    src_id: int,
    dst_slug: str,
    edge_type: str = "wikilink",
    dst_id: int | None = None,
    weight: float = 1.0,
) -> None:
    """Insert or update a graph edge."""
    conn.execute(
        """
        INSERT INTO edges (src_id, dst_slug, dst_id, edge_type, weight)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (src_id, dst_slug, edge_type) DO UPDATE SET
            dst_id = EXCLUDED.dst_id,
            weight = EXCLUDED.weight
        """,
        (src_id, dst_slug, dst_id, edge_type, weight),
    )


def log_access(conn: Any, page_id: int, source: str = "search",
               query: str | None = None, rank: int | None = None) -> None:
    """Record a page access for retrieval analytics (forgetting curve)."""
    conn.execute(
        """
        INSERT INTO access_log (page_id, source, query, rank)
        VALUES (%s, %s, %s, %s)
        """,
        (page_id, source, query, rank),
    )


def search(
    query: str,
    limit: int = 10,
    project: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
    embedding: list[float] | None = None,
    k_rrf: int = 60,
) -> list[dict]:
    """Run a hybrid search on PostgreSQL. Returns list of result dicts.

    If semantic=True and embedding is provided, runs full triple-fusion
    (BM25 + vector + graph). Otherwise runs BM25-only.
    """
    if not pg_available():
        return []

    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "project": project,
        "as_of": as_of,
    }

    if semantic and embedding is not None:
        params["embedding"] = embedding
        params["k_rrf"] = k_rrf
        sql = HYBRID_SEARCH_SQL
    else:
        sql = BM25_ONLY_SQL

    try:
        with db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "slug": row[1],
            "path": f"knowledge/notes/{row[1]}.md",
            "title": row[2] or "",
            "summary": (row[3] or "")[:120],
            "project": row[4] or "",
            "type": row[5] or "",
            "timestamp": str(row[6])[:10] if row[6] else "",
            "score": round(float(row[7]), 4) if len(row) > 7 else 0.0,
        })
    return results


def page_count() -> int:
    """Return number of active pages in PostgreSQL, or 0 if unavailable."""
    if not pg_available():
        return 0
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pages WHERE status = 'active'"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def drop_all(conn: Any) -> None:
    """Drop all llm-wiki tables (for testing / full rebuild)."""
    conn.execute("DROP TABLE IF EXISTS access_log CASCADE")
    conn.execute("DROP TABLE IF EXISTS edges CASCADE")
    conn.execute("DROP TABLE IF EXISTS pages_vec CASCADE")
    conn.execute("DROP TABLE IF EXISTS pages CASCADE")
    conn.execute("DROP TABLE IF EXISTS schema_meta CASCADE")


if __name__ == "__main__":
    if pg_available():
        count = page_count()
        print(f"PostgreSQL: {count} active pages indexed.")
    else:
        print("PostgreSQL not available. Falling back to SQLite.")
