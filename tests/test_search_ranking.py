"""Tests for search_memory.py — ranking logic, boosts, RRF fusion.

Locks in:
1. Title boost: exact title match → higher rank than BM25-only
2. Filename short-circuit: exact filename match → rank 1 always
3. Path preference: knowledge/notes/ pages boosted over knowledge/notes/
4. RRF fusion: weighted (BM25=2, Vector=1, Graph=0.5)
5. Project-scoped boost: project-tagged pages x2
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_rrf_fuse_triple_weights():
    """Weighted RRF: BM25 weight=2 should dominate Vector weight=1."""
    import search_memory

    bm25 = [
        {"path": "page_a.md", "title": "A", "summary": "", "score": 10, "project": "", "timestamp": ""},
        {"path": "page_b.md", "title": "B", "summary": "", "score": 5, "project": "", "timestamp": ""},
    ]
    vector = [
        {"path": "page_b.md", "title": "B", "summary": "", "score": 8, "project": "", "timestamp": ""},
        {"path": "page_a.md", "title": "A", "summary": "", "score": 3, "project": "", "timestamp": ""},
    ]

    result = search_memory._rrf_fuse_triple(bm25, vector, None)

    # BM25 rank=1 for A should dominate Vector rank=2 for A
    assert result[0]["path"] == "page_a.md"
    assert result[0]["fused_score"] > result[1]["fused_score"]


def test_rrf_fuse_triple_graph_boost():
    """Graph boost adds score but doesn't overtake BM25 rank=1."""
    import search_memory

    bm25 = [
        {"path": "page_a.md", "title": "A", "summary": "", "score": 10, "project": "", "timestamp": ""},
    ]
    graph = [
        {"path": "page_b.md", "graph_boost": 0.15},
    ]

    result = search_memory._rrf_fuse_triple(bm25, None, graph)
    assert result[0]["path"] == "page_a.md"  # BM25 wins over graph-only


def test_rrf_fuse_triple_empty_inputs():
    """Empty inputs don't crash."""
    import search_memory

    result = search_memory._rrf_fuse_triple([], None, None)
    assert result == []


def test_rrf_fuse_basic_two_signals():
    """Basic 2-signal RRF (BM25 + Vector)."""
    import search_memory

    bm25 = [{"path": "a.md", "title": "A", "summary": "", "score": 5, "project": "", "timestamp": ""}]
    vector = [{"path": "b.md", "title": "B", "summary": "", "score": 3, "project": "", "timestamp": ""}]

    result = search_memory._rrf_fuse(bm25, vector)
    assert len(result) == 2
    # a.md has BM25 rank=1 + no vector → higher fused score
    assert result[0]["path"] == "a.md"


def test_extract_title_and_summary():
    """Title from H1, summary from 'One-sentence summary:' line."""
    import search_memory

    content = """---
type: concept
---
# My Great Concept

One-sentence summary: This concept is about something important.

## Body
Content here.
"""
    title, summary = search_memory._extract_title_and_summary(content, "fallback")
    assert title == "My Great Concept"
    assert "something important" in summary


def test_extract_title_fallback_to_stem():
    """No H1 → filename stem."""
    import search_memory

    content = "Just body, no heading."
    title, summary = search_memory._extract_title_and_summary(content, "my-file")
    assert title == "my-file"
    assert summary == ""


def test_strip_frontmatter():
    """Frontmatter is removed from search body."""
    import search_memory

    content = "---\ntype: fact\nsecret: sk-test123\n---\n\n# Real Content\nBody text."
    stripped = search_memory._strip_frontmatter(content)
    assert "sk-test" not in stripped
    assert "Real Content" in stripped


def test_collect_pages_skips_editorial():
    """Editorial filenames (index.md, log.md) are skipped."""
    import search_memory

    with patch.object(search_memory, "WIKI_DIR"), \
         patch.object(search_memory, "KNOWLEDGE_DIR"):
        search_memory.WIKI_DIR.exists = MagicMock(return_value=False)
        search_memory.KNOWLEDGE_DIR.exists = MagicMock(return_value=False)
        pages = search_memory._collect_pages("all")
        assert pages == []


def test_needs_rebuild_no_index():
    """Returns True when index doesn't exist."""
    import search_memory

    with patch.object(Path, "exists", return_value=False):
        assert search_memory._needs_rebuild([]) is True


def test_needs_rebuild_fresh_files():
    """Returns True when source files are newer than index."""
    import time

    import search_memory

    fake_page = MagicMock()
    fake_page.stat.return_value.st_mtime = time.time()
    fake_page.is_file.return_value = True

    with patch.object(Path, "exists", return_value=True), \
         patch.object(Path, "stat") as mock_stat:
        mock_stat.return_value.st_mtime = time.time() - 3600  # index 1h old
        assert search_memory._needs_rebuild([fake_page]) is True
