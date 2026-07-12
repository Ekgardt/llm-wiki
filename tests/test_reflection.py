"""Tests for reflection.py — A-MEM memory evolution (page consolidation)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reflection import REFLECTION_THRESHOLD, find_reflection_candidates  # noqa: E402


class TestFindCandidates:
    """Test finding pages that need reflection."""

    def test_no_candidates_in_empty_vault(self, tmp_path, monkeypatch):
        import reflection

        monkeypatch.setattr(reflection, "KNOWLEDGE", tmp_path / "notes")
        candidates = find_reflection_candidates()
        assert candidates == []

    def test_finds_page_with_multiple_updates(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "growing-page.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n"
            "# Growing Page\n\n"
            "Original content.\n\n"
            "## Update (2026-01-15)\nFirst update.\n\n"
            "## Update (2026-02-20)\nSecond update.\n\n"
            "## Update (2026-03-10)\nThird update.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 1
        assert candidates[0]["slug"] == "growing-page"
        assert candidates[0]["update_count"] == 3

    def test_ignores_pages_below_threshold(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "few-updates.md"
        page.write_text(
            "---\ntype: concept\n---\n\n"
            "# Few Updates\n\n"
            "Content.\n\n"
            "## Update (2026-01-15)\nOnly one.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_skips_superseded_pages(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "old.md"
        page.write_text(
            "---\ntype: decision\nstatus: superseded\n---\n\n"
            "# Old\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_skips_already_reflected_pages(self, tmp_path, monkeypatch):
        """Pages with ## History section are already reflected."""
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "reflected.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n"
            "# Reflected\n\n"
            "Clean content.\n\n"
            "## History (pre-reflection)\nOld stuff.\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_threshold_value(self):
        """Threshold should be at least 2."""
        assert REFLECTION_THRESHOLD >= 2


class TestReflectPage:
    """Test the reflect_page function."""

    def test_dry_run_returns_message(self, tmp_path):
        from reflection import reflect_page

        page = tmp_path / "test.md"
        page.write_text(
            "# Test\n\nBody.\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        result = reflect_page(page, apply=False)
        assert "candidate" in result.lower()

    def test_skip_page_below_threshold(self, tmp_path):
        from reflection import reflect_page

        page = tmp_path / "test.md"
        page.write_text("# Test\n\nNo updates here.\n", encoding="utf-8")
        result = reflect_page(page, apply=False)
        assert "skipping" in result.lower()
