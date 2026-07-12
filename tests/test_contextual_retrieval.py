"""Tests for contextual_retrieval.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from contextual_retrieval import generate_context, get_context  # noqa: E402


class TestGenerateContext:
    """Test context generation."""

    def test_deterministic_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "auth.md"
        page.write_text(
            "---\ntype: decision\nproject: llm-wiki\n---\n\n"
            "# Auth Decision\n\n"
            "One-sentence summary: We chose JWT over sessions.\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)

        ctx = generate_context("auth", use_llm=False)
        assert "Auth Decision" in ctx
        assert "JWT" in ctx
        assert "llm-wiki" in ctx

    def test_context_for_missing_page(self, tmp_path, monkeypatch):
        import contextual_retrieval

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", tmp_path / "notes")
        ctx = generate_context("nonexistent", use_llm=False)
        assert ctx == ""

    def test_context_includes_type(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "x.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n# X\n\nOne-sentence summary: Test.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)

        ctx = generate_context("x", use_llm=False)
        assert "pattern" in ctx.lower()


class TestGetContext:
    """Test cached context retrieval."""

    def test_get_existing_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        ctx_dir = tmp_path / "ctx"
        ctx_dir.mkdir()
        (ctx_dir / "test.ctx").write_text("Context for test page.", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", ctx_dir)

        result = get_context("test")
        assert result == "Context for test page."

    def test_get_missing_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", tmp_path / "ctx")
        result = get_context("nonexistent")
        assert result is None


class TestBuildAll:
    """Test batch context generation."""

    def test_build_all_deterministic(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text(
            "---\ntype: concept\n---\n\n# A\n\nOne-sentence summary: Page A.\n",
            encoding="utf-8",
        )
        (notes / "b.md").write_text(
            "---\ntype: decision\n---\n\n# B\n\nOne-sentence summary: Page B.\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", tmp_path / "ctx")

        stats = contextual_retrieval.build_all_contexts(use_llm=False, verbose=False)
        assert stats["generated"] == 2
        assert (tmp_path / "ctx" / "a.ctx").exists()
        assert (tmp_path / "ctx" / "b.ctx").exists()
