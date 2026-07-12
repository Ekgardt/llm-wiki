"""Tests for impact_analysis.py — LINK Layer (code → wiki connection)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from impact_analysis import (  # noqa: E402
    extract_symbols_from_file,
    find_stale_wiki_pages,
    format_for_advisory,
)


class TestExtractSymbols:
    """Test symbol extraction from source files."""

    def test_extract_from_python(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    pass\nclass World:\n    pass\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert "hello" in symbols
        assert "World" in symbols

    def test_extract_from_javascript(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text("function greet() {}\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert "greet" in symbols

    def test_extract_from_nonexistent(self, tmp_path):
        symbols = extract_symbols_from_file(tmp_path / "nope.py")
        assert symbols == []

    def test_extract_deduplicates(self, tmp_path):
        f = tmp_path / "dup.py"
        f.write_text("def foo():\n    foo()\n    foo()\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert symbols.count("foo") == 1


class TestFindStaleWikiPages:
    """Test finding wiki pages that reference changed symbols."""

    def test_finds_page_mentioning_symbol(self, tmp_path, monkeypatch):
        """A wiki page that mentions a changed function should be found."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "auth-decision.md"
        page.write_text(
            "---\ntype: decision\n---\n\n"
            "# Auth Decision\n\n"
            "We use verifyToken for auth.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["verifyToken"])
        assert len(results) == 1
        assert results[0]["slug"] == "auth-decision"
        assert "verifyToken" in results[0]["matched_symbols"]

    def test_no_match_returns_empty(self, tmp_path, monkeypatch):
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "unrelated.md"
        page.write_text("# Unrelated\n\nNothing about code.\n", encoding="utf-8")
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["nonexistentSymbol"])
        assert results == []

    def test_skips_superseded(self, tmp_path, monkeypatch):
        """Superseded pages should not be flagged as stale."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "old.md"
        page.write_text(
            "---\nstatus: superseded\n---\n\n"
            "# Old\n\nMentions verifyToken.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["verifyToken"])
        assert len(results) == 0

    def test_confidence_levels(self, tmp_path, monkeypatch):
        """Multiple symbol matches → high confidence."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "multi.md"
        page.write_text(
            "# Multi\n\nUses funcA, funcB, and funcC.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["funcA", "funcB", "funcC"])
        assert len(results) == 1
        assert results[0]["confidence"] == "high"

    def test_word_boundary_matching(self, tmp_path, monkeypatch):
        """Symbol 'auth' should not match 'authentication'."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "page.md"
        page.write_text("# Page\n\nAbout authentication.\n", encoding="utf-8")
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["auth"])
        # 'auth' as a word boundary should NOT match 'authentication'
        assert len(results) == 0


class TestFormatForAdvisory:
    """Test advisory formatting for SessionStart."""

    def test_empty_stale_returns_empty(self):
        result = format_for_advisory({"stale_pages": [], "summary": "nothing"})
        assert result == ""

    def test_formats_pages(self):
        impact = {
            "summary": "3 files, 5 symbols, 2 stale pages.",
            "stale_pages": [
                {"slug": "page-a", "confidence": "high", "reason": "mentions 3 symbols", "matched_symbols": ["a", "b", "c"]},
                {"slug": "page-b", "confidence": "medium", "reason": "mentions 1 symbol", "matched_symbols": ["d"]},
            ],
        }
        result = format_for_advisory(impact)
        assert "Code-Knowledge Impact" in result
        assert "page-a" in result
        assert "page-b" in result

    def test_limits_to_max_pages(self):
        impact = {
            "summary": "many changes",
            "stale_pages": [
                {"slug": f"page-{i}", "confidence": "medium", "reason": "test", "matched_symbols": ["x"]}
                for i in range(10)
            ],
        }
        result = format_for_advisory(impact, max_pages=3)
        assert "page-0" in result
        assert "page-2" in result
        assert "7 more" in result
