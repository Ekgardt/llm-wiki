"""Tests for lance_store.py — LanceDB embedded vector backend."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestGracefulDegradation:
    """LanceDB functions must degrade gracefully when not installed."""

    def test_have_lancedb_returns_bool(self):
        from lance_store import have_lancedb
        assert isinstance(have_lancedb(), bool)

    def test_vector_search_returns_empty_without_lancedb(self):
        from lance_store import vector_search
        results = vector_search([0.1] * 384)
        assert isinstance(results, list)
        assert len(results) == 0

    def test_vector_count_returns_zero_without_lancedb(self):
        from lance_store import vector_count
        assert isinstance(vector_count(), int)
        assert vector_count() == 0

    def test_upsert_returns_zero_without_lancedb(self):
        from lance_store import upsert_vectors
        result = upsert_vectors(
            paths=["test"], titles=["T"], summaries=["S"],
            projects=["p"], timestamps=["2026-01-01"],
            vectors=[[0.1] * 384],
        )
        assert result == 0


class TestModuleStructure:
    """Verify module exports are correct."""

    def test_lancedb_dir_path(self):
        from lance_store import LANCEDB_DIR
        assert "lancedb" in str(LANCEDB_DIR).lower()
        assert "cache" in str(LANCEDB_DIR).lower()

    def test_table_name(self):
        from lance_store import TABLE_NAME
        assert TABLE_NAME == "pages_vec"

    def test_embedding_dim(self):
        from lance_store import EMBEDDING_DIM
        assert EMBEDDING_DIM == 384

    def test_all_functions_exist(self):
        from lance_store import (
            have_lancedb,
            upsert_vectors,
            vector_count,
            vector_search,
        )
        assert callable(have_lancedb)
        assert callable(upsert_vectors)
        assert callable(vector_search)
        assert callable(vector_count)


class TestSearchIntegration:
    """Test search_memory.py falls back correctly when LanceDB unavailable."""

    def test_search_works_without_lancedb(self):
        """Search must work with numpy fallback when LanceDB not installed."""
        from search_memory import search
        results = search("test", limit=5)
        assert isinstance(results, list)

    def test_search_semantic_works_without_lancedb(self):
        from search_memory import search
        results = search("test", limit=5, semantic=True)
        assert isinstance(results, list)
