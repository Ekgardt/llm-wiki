"""Tests for build_guardrails.py — rule extraction and dedup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_knowledge_dir(tmp_path, monkeypatch):
    """Set up a temporary knowledge directory."""
    import build_guardrails

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    monkeypatch.setattr(build_guardrails, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_guardrails, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(build_guardrails, "GUARDRAILS_FILE", tmp_path / "guardrails.md")
    monkeypatch.setattr(build_guardrails, "ROOT", tmp_path)
    (tmp_path / "feedback").mkdir()
    return knowledge


def make_page(path: Path, page_type: str, title: str, summary: str):
    """Create a knowledge page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\n---\n\n# {title}\n\nOne-sentence summary: {summary}\n\n## Body\nContent.\n",
        encoding="utf-8",
    )


def test_collect_correction_type(fake_knowledge_dir):
    """Pages with type=correction are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/correction1.md",
              "correction", "Use JWT", "Always use JWT instead of sessions for auth")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1
    assert corrections[0]["type"] == "correction"


def test_collect_preference_type(fake_knowledge_dir):
    """Pages with type=preference are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/pref1.md",
              "preference", "Short answers", "I prefer concise responses")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1
    assert "concise" in corrections[0]["summary"]


def test_collect_pattern_with_imperative(fake_knowledge_dir):
    """Patterns with 'do not' / 'always' in summary are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/rule1.md",
              "pattern", "Backlink rule", "Always add reciprocal backlinks when creating new pages")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1


def test_collect_ignores_plain_patterns(fake_knowledge_dir):
    """Patterns without imperative language are NOT collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/info.md",
              "pattern", "Mirror pipelines", "This pattern describes reusing existing infrastructure shapes")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 0


def test_collect_filters_by_project(fake_knowledge_dir):
    """Project filter works."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "correction", "Rule A", "Always do X",
              )
    # Add project to frontmatter
    path = fake_knowledge_dir / "patterns/c1.md"
    content = path.read_text()
    content = content.replace("---\n", "---\nproject: project-a\n", 1)
    path.write_text(content)

    # Should find with project filter
    assert len(build_guardrails._collect_corrections("project-a")) == 1
    # Should NOT find with different project filter
    assert len(build_guardrails._collect_corrections("project-b")) == 0


def test_build_guardrails_formats_output(fake_knowledge_dir):
    """build_guardrails produces formatted markdown."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "correction", "Use JWT", "Always use JWT instead of sessions for auth")
    make_page(fake_knowledge_dir / "patterns/p1.md",
              "preference", "Short answers", "I prefer concise responses")

    result = build_guardrails.build_guardrails()
    assert "Guard rails" in result
    assert "CORRECTION" in result
    assert "PREFERENCE" in result


def test_build_guardrails_empty_returns_empty(fake_knowledge_dir):
    """No corrections → empty string."""
    import build_guardrails

    assert build_guardrails.build_guardrails() == ""


def test_build_guardrails_dedup(fake_knowledge_dir):
    """Duplicate summaries are deduplicated."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "correction", "A", "Always use JWT instead of sessions for auth")
    make_page(fake_knowledge_dir / "patterns/c2.md",
              "correction", "B", "Always use JWT instead of sessions for auth")  # same summary

    result = build_guardrails.build_guardrails()
    # Should appear only once after dedup
    assert result.count("Always use JWT") == 1
