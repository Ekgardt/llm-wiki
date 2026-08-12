"""Tests for graph_neighbors.py — link resolution and boost calculation."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_graph():
    """Reset the module-level cache before each test."""
    import graph_neighbors
    graph_neighbors._link_graph_cache = None
    yield graph_neighbors
    graph_neighbors._link_graph_cache = None


def test_resolve_wikilink_exact_match(fake_graph):
    """Wikilink target resolves to an existing file."""
    with patch.object(fake_graph, "KNOWLEDGE_DIR") as knowledge:
        knowledge.exists.return_value = True
        fake_file = MagicMock()
        fake_file.exists.return_value = True
        fake_file.is_file.return_value = True
        fake_file.read_text.return_value = "# Some Page\n\nBody text."
        fake_file.relative_to.return_value.as_posix.return_value = "some-page.md"
        fake_file.resolve.return_value = fake_file
        knowledge.rglob.return_value = [fake_file]
        result = fake_graph._resolve_wikilink("some-page")
        assert result == "some-page.md"


def test_resolve_wikilink_not_found(fake_graph):
    """Non-existent target returns None."""
    with patch.object(Path, "exists", return_value=False):
        result = fake_graph._resolve_wikilink("nonexistent")
        assert result is None


def test_get_neighbors_returns_links(fake_graph):
    """get_neighbors returns outbound links for a page."""
    fake_graph._link_graph_cache = {
        "page_a.md": ["page_b.md", "page_c.md"],
        "page_b.md": ["page_a.md"],
    }
    neighbors = fake_graph.get_neighbors("page_a.md")
    assert set(neighbors) == {"page_b.md", "page_c.md"}


def test_get_neighbors_empty(fake_graph):
    """Page with no links returns empty list."""
    fake_graph._link_graph_cache = {"page_a.md": []}
    assert fake_graph.get_neighbors("page_a.md") == []


def test_get_reverse_neighbors(fake_graph):
    """Reverse neighbors: who links TO this page."""
    fake_graph._link_graph_cache = {
        "page_b.md": ["page_a.md"],
        "page_c.md": ["page_a.md", "page_d.md"],
    }
    reverse = fake_graph.get_reverse_neighbors("page_a.md")
    assert set(reverse) == {"page_b.md", "page_c.md"}


def test_boost_graph_neighbors_prioritizes_close(fake_graph):
    """Closer neighbors (rank 0) get more boost than distant ones."""
    fake_graph._link_graph_cache = {
        "result1.md": ["neighbor1.md", "neighbor2.md"],
    }
    bm25_results = [{"path": "result1.md", "score": 10}]
    boosts = fake_graph.boost_graph_neighbors(bm25_results, None)

    # neighbor1 (rank 0) should have more boost than neighbor2 (rank 1)
    boost_map = {b["path"]: b["graph_boost"] for b in boosts}
    assert "neighbor1.md" in boost_map, "neighbor1 should be boosted"
    assert "neighbor2.md" in boost_map, "neighbor2 should be boosted"
    assert boost_map["neighbor1.md"] >= boost_map["neighbor2.md"]


def test_boost_empty_results(fake_graph):
    """Empty BM25 results → empty or minimal boosts."""
    fake_graph._link_graph_cache = {}
    boosts = fake_graph.boost_graph_neighbors([], None)
    assert len(boosts) == 0


def test_rebuild_graph_cache_returns_edges(fake_graph):
    """rebuild_graph_cache returns edge count and populates cache."""
    with patch.object(fake_graph, "_build_link_graph", return_value={"a.md": ["b.md"], "b.md": []}):
        edges = fake_graph.rebuild_graph_cache()
        assert edges == 1  # only a→b


def test_build_link_graph_extracts_wikilinks(fake_graph, tmp_path, monkeypatch):
    """_build_link_graph finds [[wikilinks]] in markdown files."""
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)

    def write_page(name: str, title: str, body: str = "") -> Path:
        page = notes / name
        page.write_text(
            "---\ntype: pattern\n---\n\n"
            f"# {title}\n\n{body}\n",
            encoding="utf-8",
        )
        return page

    source = write_page("page-a.md", "Page A", "See [[page-b]] and [[page-c]].")
    write_page("page-b.md", "Page B")
    write_page("page-c.md", "Page C")
    monkeypatch.setattr(fake_graph, "ROOT", tmp_path)
    monkeypatch.setattr(fake_graph, "KNOWLEDGE_DIR", notes)
    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    graph = fake_graph._build_link_graph(selection)

    assert source.relative_to(tmp_path).as_posix() in graph
    assert len(graph[source.relative_to(tmp_path).as_posix()]) == 2


@pytest.mark.parametrize(
    "frontmatter",
    (
        'status: "super\\x73eded"',
        'status: "\\qactive"',
        'project: "\\qbeta"\nstatus: active',
    ),
)
def test_graph_fails_closed_with_shared_sensitive_frontmatter_parser(
    fake_graph,
    frontmatter,
):
    content = f"---\n{frontmatter}\n---\n\n# Hidden\n"

    assert fake_graph._is_inactive(content) is True
