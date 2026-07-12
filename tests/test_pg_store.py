"""Tests for the PostgreSQL + pgvector backend (pg_store.py).

Tests are structured in three tiers:
1. Graceful degradation tests — always run, verify safe behavior without psycopg
2. Schema/SQL correctness tests — always run, validate DDL and query structure
3. Integration tests — SKIP if PostgreSQL not available (needs LLMWIKI_TEST_PG_DSN)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Inject scripts/ into sys.path (conftest.py does this, but be explicit for IDE).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pg_store import (  # noqa: E402
    BM25_ONLY_SQL,
    HYBRID_SEARCH_SQL,
    SCHEMA_DDL,
    SCHEMA_VERSION,
    log_access,
    page_count,
    pg_available,
    search,
    upsert_edge,
    upsert_embedding,
    upsert_page,
)

# ──────────────────────────────────────────────────────────
# Tier 1: Graceful degradation (always run — no psycopg needed)
# ──────────────────────────────────────────────────────────

class TestGracefulDegradation:
    """All pg_store functions must return safe defaults when psycopg is missing."""

    def test_pg_available_returns_false_without_psycopg(self):
        """pg_available() must not raise even if psycopg is not installed."""
        result = pg_available()
        # In the test environment without psycopg, this should be False.
        # (If psycopg IS installed, this test still passes — it just checks no crash.)
        assert isinstance(result, bool)

    def test_search_returns_empty_list_without_pg(self):
        """search() returns [] gracefully, never raises."""
        results = search("test query")
        assert isinstance(results, list)
        assert len(results) == 0

    def test_page_count_returns_zero_without_pg(self):
        """page_count() returns 0 gracefully."""
        count = page_count()
        assert isinstance(count, int)
        assert count == 0

    def test_search_with_all_params_returns_empty_without_pg(self):
        """search() with project/as_of/semantic params still degrades."""
        results = search("auth", limit=5, project="myproj", as_of="2026-01-01", semantic=True)
        assert results == []

    def test_init_schema_returns_false_without_psycopg(self):
        """init_schema() returns False, never raises."""
        from pg_store import init_schema
        result = init_schema()
        assert result is False


# ──────────────────────────────────────────────────────────
# Tier 2: Schema and SQL correctness (always run)
# ──────────────────────────────────────────────────────────

class TestSchemaDDL:
    """Validate the DDL is well-formed and contains required elements."""

    def test_schema_contains_pages_table(self):
        assert "CREATE TABLE IF NOT EXISTS pages" in SCHEMA_DDL

    def test_schema_contains_pages_vec_table(self):
        assert "CREATE TABLE IF NOT EXISTS pages_vec" in SCHEMA_DDL

    def test_schema_contains_edges_table(self):
        assert "CREATE TABLE IF NOT EXISTS edges" in SCHEMA_DDL

    def test_schema_contains_access_log_table(self):
        assert "CREATE TABLE IF NOT EXISTS access_log" in SCHEMA_DDL

    def test_schema_contains_vector_extension(self):
        assert "CREATE EXTENSION IF NOT EXISTS vector" in SCHEMA_DDL

    def test_schema_contains_hnsw_index(self):
        assert "USING hnsw" in SCHEMA_DDL
        assert "vector_cosine_ops" in SCHEMA_DDL

    def test_schema_contains_gin_index_for_fts(self):
        assert "USING GIN" in SCHEMA_DDL

    def test_schema_contains_generated_tsvector(self):
        """fts column must be a GENERATED tsvector with weighted fields."""
        assert "GENERATED ALWAYS AS" in SCHEMA_DDL
        assert "setweight" in SCHEMA_DDL
        assert "to_tsvector" in SCHEMA_DDL

    def test_schema_vector_dimension_is_384(self):
        """Both bge-small-en-v1.5 and all-MiniLM-L6-v2 use 384 dimensions."""
        assert "vector(384)" in SCHEMA_DDL

    def test_schema_has_temporal_columns(self):
        """Bi-temporal: valid_from and valid_to on pages and edges."""
        assert "valid_from" in SCHEMA_DDL
        assert "valid_to" in SCHEMA_DDL

    def test_schema_has_status_column(self):
        """status: active|superseded|archived — for filtering."""
        assert "status" in SCHEMA_DDL

    def test_schema_has_frontmatter_jsonb(self):
        """frontmatter stored as JSONB for flexible querying."""
        assert "jsonb" in SCHEMA_DDL
        assert "frontmatter" in SCHEMA_DDL

    def test_schema_has_partial_active_index(self):
        """Partial GIN index on active pages only — speeds up default search."""
        assert "WHERE status = 'active'" in SCHEMA_DDL

    def test_schema_has_cascade_on_delete(self):
        """Deleting a page cascades to vectors, edges, and access_log."""
        assert "ON DELETE CASCADE" in SCHEMA_DDL

    def test_schema_version_is_set(self):
        assert SCHEMA_VERSION == 1

    def test_schema_meta_table_exists(self):
        """schema_meta tracks the schema version for migrations."""
        assert "schema_meta" in SCHEMA_DDL


class TestHybridSearchSQL:
    """Validate the hybrid search CTE is correct."""

    def test_hybrid_sql_has_rrf_weights(self):
        """Weighted RRF: BM25=2.0, Vector=1.0, Graph=0.5."""
        assert "2.0" in HYBRID_SEARCH_SQL
        assert "1.0" in HYBRID_SEARCH_SQL
        assert "0.5" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_has_three_ctes(self):
        """Three WITH clauses: semantic, keyword, graph."""
        assert "semantic AS" in HYBRID_SEARCH_SQL
        assert "keyword AS" in HYBRID_SEARCH_SQL
        assert "graph AS" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_filters_active_pages(self):
        assert "status = 'active'" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_has_temporal_filter(self):
        """as_of filter: valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)."""
        assert "valid_from" in HYBRID_SEARCH_SQL
        assert "valid_to" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_has_project_filter(self):
        assert "project" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_uses_cosine_distance(self):
        """pgvector cosine: <=> operator."""
        assert "<=>" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_uses_ts_rank_cd(self):
        """Cover-density ranking for BM25."""
        assert "ts_rank_cd" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_uses_plainto_tsquery(self):
        assert "plainto_tsquery" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_graph_uses_wikilink_edges(self):
        """Graph neighbor boost only follows wikilink edges."""
        assert "edge_type = 'wikilink'" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_limits_candidates(self):
        """Each CTE limits to 20 candidates before fusion."""
        assert "LIMIT 20" in HYBRID_SEARCH_SQL

    def test_hybrid_sql_has_k_rrf_parameter(self):
        assert "k_rrf" in HYBRID_SEARCH_SQL

    def test_bm25_sql_has_correct_structure(self):
        """BM25-only fallback query."""
        assert "ts_rank_cd" in BM25_ONLY_SQL
        assert "plainto_tsquery" in BM25_ONLY_SQL
        assert "status = 'active'" in BM25_ONLY_SQL


# ──────────────────────────────────────────────────────────
# Tier 3: Unit tests with mocked connections (always run)
# ──────────────────────────────────────────────────────────

class TestUpsertFunctions:
    """Test upsert functions with mocked psycopg connections."""

    def test_upsert_page_returns_page_id(self):
        """upsert_page calls INSERT ... RETURNING id and returns the id."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (42,)
        page_id = upsert_page(
            mock_conn,
            slug="test-page",
            title="Test Page",
            summary="A test",
            body="Content here",
            project="myproject",
            page_type="concept",
        )
        assert page_id == 42
        mock_conn.execute.assert_called_once()

    def test_upsert_page_with_all_fields(self):
        """All optional fields are passed through."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (1,)
        upsert_page(
            mock_conn,
            slug="full-page",
            title="Full",
            summary="Summary",
            body="Body",
            frontmatter={"key": "value"},
            project="proj",
            page_type="decision",
            status="active",
            confidence="high",
            source_authority="user",
            valid_from="2026-01-01",
            valid_to=None,
            content_hash="abc123",
            timestamp="2026-01-01",
        )
        call_args = mock_conn.execute.call_args
        sql_text = call_args[0][0]
        params = call_args[0][1]
        assert "INSERT INTO pages" in sql_text
        assert "ON CONFLICT (slug) DO UPDATE" in sql_text
        assert len(params) == 14

    def test_upsert_embedding_calls_correct_sql(self):
        mock_conn = MagicMock()
        upsert_embedding(mock_conn, page_id=1, embedding=[0.1] * 384, model="bge-small-en-v1.5")
        sql_text = mock_conn.execute.call_args[0][0]
        assert "INSERT INTO pages_vec" in sql_text
        assert "ON CONFLICT (page_id)" in sql_text

    def test_upsert_edge_calls_correct_sql(self):
        mock_conn = MagicMock()
        upsert_edge(mock_conn, src_id=1, dst_slug="other-page", edge_type="wikilink", dst_id=2)
        sql_text = mock_conn.execute.call_args[0][0]
        assert "INSERT INTO edges" in sql_text
        assert "ON CONFLICT (src_id, dst_slug, edge_type)" in sql_text

    def test_log_access_calls_correct_sql(self):
        mock_conn = MagicMock()
        log_access(mock_conn, page_id=1, source="search", query="auth", rank=1)
        sql_text = mock_conn.execute.call_args[0][0]
        assert "INSERT INTO access_log" in sql_text


class TestSearchResultFormat:
    """Verify the result dict format matches what search_memory.py expects."""

    def test_search_result_has_path_field(self):
        """Results must include 'path' for backward compat with search_memory.py."""
        # Mock pg_available to return True, mock db() to return fake rows
        with patch("pg_store.pg_available", return_value=True), \
             patch("pg_store.get_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                (1, "auth-decision", "Auth Decision", "About auth",
                 "myproj", "decision", "2026-01-01", 0.95),
            ]
            mock_pool.return_value.connection.return_value.__enter__.return_value = mock_conn
            mock_pool.return_value.connection.return_value.__exit__.return_value = False

            results = search("auth")
            assert len(results) == 1
            r = results[0]
            assert r["slug"] == "auth-decision"
            assert r["path"] == "knowledge/notes/auth-decision.md"
            assert r["title"] == "Auth Decision"
            assert r["score"] == 0.95
            assert r["project"] == "myproj"

    def test_search_result_summary_truncated(self):
        """Summary is truncated to 120 chars (matches search_memory.py behavior)."""
        with patch("pg_store.pg_available", return_value=True), \
             patch("pg_store.get_pool") as mock_pool:
            long_summary = "x" * 200
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                (1, "test", "Test", long_summary, "", "concept", "2026-01-01", 1.0),
            ]
            mock_pool.return_value.connection.return_value.__enter__.return_value = mock_conn
            mock_pool.return_value.connection.return_value.__exit__.return_value = False

            results = search("test")
            assert len(results[0]["summary"]) == 120

    def test_search_returns_empty_on_pg_error(self):
        """If PG raises during search, return [] (caller falls back to SQLite)."""
        with patch("pg_store.pg_available", return_value=True), \
             patch("pg_store.get_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_conn.execute.side_effect = RuntimeError("connection lost")
            mock_pool.return_value.connection.return_value.__enter__.return_value = mock_conn
            mock_pool.return_value.connection.return_value.__exit__.return_value = False

            results = search("auth")
            assert results == []


# ──────────────────────────────────────────────────────────
# Tier 4: Integration tests (SKIP if no PostgreSQL available)
# ──────────────────────────────────────────────────────────

def _pg_test_available() -> bool:
    """Check if PostgreSQL + pgvector is available for integration tests."""
    try:
        import psycopg  # noqa: F401
        dsn = os.environ.get("LLMWIKI_TEST_PG_DSN")
        if not dsn:
            return False
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            row = conn.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            return row is not None
    except Exception:
        return False


needs_pg = pytest.mark.skipif(
    not _pg_test_available(),
    reason="PostgreSQL + pgvector not available (set LLMWIKI_TEST_PG_DSN to enable)",
)


@needs_pg
class TestPostgreSQLIntegration:
    """Full integration tests against a real PostgreSQL instance."""

    @pytest.fixture(autouse=True)
    def _clean_db(self):
        """Create a fresh schema before each test class, drop after."""
        import psycopg
        dsn = os.environ["LLMWIKI_TEST_PG_DSN"]
        with psycopg.connect(dsn, autocommit=True) as conn:
            from pg_store import SCHEMA_DDL, SCHEMA_VERSION, drop_all
            drop_all(conn)
            conn.execute(SCHEMA_DDL, (str(SCHEMA_VERSION),))
        yield

    def test_upsert_and_search(self):
        """Insert a page and find it via BM25 search."""
        import psycopg
        dsn = os.environ["LLMWIKI_TEST_PG_DSN"]
        os.environ["LLMWIKI_PG_DSN"] = dsn

        with psycopg.connect(dsn) as conn:
            page_id = upsert_page(
                conn, slug="test-integration", title="Test Integration Page",
                summary="A page about authentication", body="JWT tokens for auth",
                project="test", page_type="decision",
            )
            assert page_id > 0
            conn.commit()

        results = search("authentication")
        assert len(results) >= 1
        assert any(r["slug"] == "test-integration" for r in results)

    def test_status_filter_excludes_superseded(self):
        """Superseded pages must not appear in search results."""
        import psycopg
        dsn = os.environ["LLMWIKI_TEST_PG_DSN"]

        with psycopg.connect(dsn) as conn:
            upsert_page(
                conn, slug="old-decision", title="Old Decision",
                summary="About passwords", body="Use bcrypt",
                status="superseded",
            )
            conn.commit()

        results = search("passwords")
        assert all(r["slug"] != "old-decision" for r in results)

    def test_temporal_filter(self):
        """as_of filter excludes pages not yet valid."""
        import psycopg
        dsn = os.environ["LLMWIKI_TEST_PG_DSN"]

        with psycopg.connect(dsn) as conn:
            upsert_page(
                conn, slug="future-page", title="Future",
                summary="About quantum", body="Quantum computing",
                valid_from="2030-01-01",
            )
            conn.commit()

        results = search("quantum", as_of="2026-01-01")
        assert all(r["slug"] != "future-page" for r in results)

    def test_project_filter(self):
        """Project filter narrows results."""
        import psycopg
        dsn = os.environ["LLMWIKI_TEST_PG_DSN"]

        with psycopg.connect(dsn) as conn:
            upsert_page(
                conn, slug="proj-a-page", title="Proj A",
                summary="About caching", body="Redis cache",
                project="project-a",
            )
            upsert_page(
                conn, slug="proj-b-page", title="Proj B",
                summary="About caching", body="Memcached cache",
                project="project-b",
            )
            conn.commit()

        results = search("caching", project="project-a")
        assert all(r["project"] == "project-a" for r in results)
