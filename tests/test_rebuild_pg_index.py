"""Tests for rebuild_pg_index.py — Markdown → PostgreSQL pipeline.

Tests cover:
1. Page collection (collect_pages)
2. Frontmatter parsing (parse_page)
3. Graceful degradation (rebuild without PG)
4. Integration (skip without PG)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rebuild_pg_index import collect_pages, parse_page  # noqa: E402


class TestCollectPages:
    """Test page collection from knowledge/notes/."""

    def test_collect_pages_returns_list(self):
        pages = collect_pages()
        assert isinstance(pages, list)

    def test_collect_pages_excludes_index(self):
        """index.md, log.md, README.md should be excluded."""
        pages = collect_pages()
        names = {p.name for p in pages}
        assert "index.md" not in names
        assert "log.md" not in names

    def test_collect_pages_excludes_archive(self):
        """Pages in archive/ subdirectory should be excluded."""
        pages = collect_pages()
        for p in pages:
            assert "archive" not in p.parts, f"Archived page found: {p}"


class TestParsePage:
    """Test frontmatter parsing."""

    def test_parse_page_extracts_title(self, tmp_path):
        md = tmp_path / "test-page.md"
        md.write_text(
            "---\n"
            "type: decision\n"
            "---\n\n"
            "# Test Decision Page\n\n"
            "One-sentence summary: We decided to test.\n\n"
            "Body text here.\n",
            encoding="utf-8",
        )
        rec = parse_page(md)
        assert rec["title"] == "Test Decision Page"
        assert rec["summary"] == "We decided to test."
        assert rec["page_type"] == "decision"

    def test_parse_page_extracts_status(self, tmp_path):
        md = tmp_path / "old.md"
        md.write_text(
            "---\n"
            "type: decision\n"
            "status: superseded\n"
            "---\n\n"
            "# Old Decision\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        rec = parse_page(md)
        assert rec["status"] == "superseded"

    def test_parse_page_extracts_project(self, tmp_path):
        md = tmp_path / "proj.md"
        md.write_text(
            "---\n"
            "type: pattern\n"
            "project: my-cool-project\n"
            "---\n\n"
            "# Pattern\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        rec = parse_page(md)
        assert rec["project"] == "my-cool-project"

    def test_parse_page_extracts_wikilinks(self, tmp_path):
        md = tmp_path / "linked.md"
        md.write_text(
            "---\n"
            "type: concept\n"
            "---\n\n"
            "# Linked Page\n\n"
            "See [[other-page]] and [[Display Name|display-slug]].\n",
            encoding="utf-8",
        )
        rec = parse_page(md)
        assert "other-page" in rec["link_slugs"]
        assert "display-slug" in rec["link_slugs"]

    def test_parse_page_extracts_temporal(self, tmp_path):
        md = tmp_path / "temporal.md"
        md.write_text(
            "---\n"
            "type: decision\n"
            "timestamp: 2026-01-15\n"
            "valid_to: 2026-06-01\n"
            "---\n\n"
            "# Temporal Decision\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        rec = parse_page(md)
        assert rec["timestamp"] == "2026-01-15"
        assert rec["valid_to"] == "2026-06-01"

    def test_parse_page_has_content_hash(self, tmp_path):
        md = tmp_path / "hashed.md"
        md.write_text("---\ntype: concept\n---\n\n# Hashed\n\nBody.\n", encoding="utf-8")
        rec = parse_page(md)
        assert rec["content_hash"] is not None
        assert len(rec["content_hash"]) > 0

    def test_parse_page_defaults(self, tmp_path):
        """Pages without frontmatter get safe defaults."""
        md = tmp_path / "bare.md"
        md.write_text("# Bare Page\n\nNo frontmatter.\n", encoding="utf-8")
        rec = parse_page(md)
        assert rec["title"] == "Bare Page"
        assert rec["status"] == "active"
        assert rec["page_type"] == "concept"


class TestRebuildGracefulDegradation:
    """Test rebuild_pg degrades gracefully without PostgreSQL."""

    def test_rebuild_returns_error_without_pg(self):
        """rebuild_pg should return error dict, not crash."""
        from rebuild_pg_index import rebuild_pg

        stats = rebuild_pg(verbose=False)
        assert "error" in stats
        assert stats["pages"] == 0


# Integration test — skip without PostgreSQL.
def _pg_test_available() -> bool:
    try:
        import os

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


@pytest.mark.skipif(
    not _pg_test_available(),
    reason="PostgreSQL + pgvector not available (set LLMWIKI_TEST_PG_DSN to enable)",
)
class TestRebuildIntegration:
    """Full rebuild integration tests against real PostgreSQL."""

    def test_full_rebuild_populates_pages(self):
        from pg_store import page_count
        from rebuild_pg_index import rebuild_pg

        stats = rebuild_pg(semantic=False, verbose=False)
        assert stats["pages"] > 0
        assert page_count() > 0

    def test_rebuild_with_embeddings(self):
        from rebuild_pg_index import rebuild_pg

        stats = rebuild_pg(semantic=True, verbose=False)
        assert stats["pages"] > 0
