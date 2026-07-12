"""Tests for build_tiers.py — L0/L1/L2 progressive disclosure."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_tiers import _deterministic_l1, get_l0, get_l2, get_tier  # noqa: E402


class TestL0:
    """Test L0 (one-sentence summary) extraction."""

    def test_l0_from_summary_line(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "test.md"
        page.write_text(
            "---\ntype: concept\n---\n\n"
            "# Test Page\n\n"
            "One-sentence summary: This is a test about auth.\n\n"
            "Body text.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l0("test")
        assert "This is a test about auth." in result

    def test_l0_fallback_to_first_sentence(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "bare.md"
        page.write_text("# Bare Page\n\nFirst sentence here. Second sentence.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l0("bare")
        assert "First sentence" in result

    def test_l0_nonexistent_page(self, tmp_path, monkeypatch):
        import build_tiers

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", tmp_path / "notes")
        result = get_l0("nonexistent")
        assert result == ""


class TestL2:
    """Test L2 (full page content)."""

    def test_l2_reads_full_content(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "full.md"
        page.write_text("# Full Page\n\nAll content here.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l2("full")
        assert "All content here." in result

    def test_l2_nonexistent(self, tmp_path, monkeypatch):
        import build_tiers

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", tmp_path / "notes")
        assert get_l2("missing") == ""


class TestDeterministicL1:
    """Test deterministic L1 extraction (no LLM)."""

    def test_extracts_key_sections(self):
        body = (
            "# Page\n\n"
            "One-sentence summary: Test page.\n\n"
            "## Key Points\n\n"
            "- Point A\n"
            "- Point B\n\n"
            "## Details\n\n"
            "Detailed info.\n"
        )
        result = _deterministic_l1("test", body, "Test page.")
        assert "Test page." in result
        assert "Key Points" in result or "Point A" in result

    def test_stops_at_history(self):
        body = (
            "# Page\n\n"
            "Content.\n\n"
            "## History (pre-reflection)\n\n"
            "Old stuff that shouldn't be in L1.\n"
        )
        result = _deterministic_l1("test", body, "Summary.")
        assert "Old stuff" not in result

    def test_truncates_long_content(self):
        body = "# Page\n\n" + "A" * 5000 + "\n"
        result = _deterministic_l1("test", body, "Summary.")
        assert len(result) < 3000  # Should be truncated


class TestGetTier:
    """Test the tier dispatcher."""

    def test_auto_returns_best_available(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "tier.md"
        page.write_text(
            "# Tier Test\n\nOne-sentence summary: Tier test.\n\nBody.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path / "tiers")

        result = get_tier("tier", level="auto")
        assert result["content"] is not None
        assert "l0" in result["available"]

    def test_l0_level(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "x.md"
        page.write_text("# X\n\nOne-sentence summary: Summary X.\n\nBody.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_tier("x", level="l0")
        assert "Summary X." in result["content"]

    def test_l2_level(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "y.md"
        page.write_text("# Y\n\nFull content.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_tier("y", level="l2")
        assert "Full content." in result["content"]


class TestBuildAllTiers:
    """Test batch L1 generation."""

    def test_build_all_deterministic(self, tmp_path, monkeypatch):
        """Build L1 for all pages using deterministic mode."""
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("# A\n\nOne-sentence summary: Page A.\n\nBody A.\n", encoding="utf-8")
        (notes / "b.md").write_text("# B\n\nOne-sentence summary: Page B.\n\nBody B.\n", encoding="utf-8")

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path / "tiers")

        stats = build_tiers.build_all_tiers(use_llm=False, verbose=False)
        assert stats["generated"] == 2
        assert (tmp_path / "tiers" / "a.l1.md").exists()
        assert (tmp_path / "tiers" / "b.l1.md").exists()
