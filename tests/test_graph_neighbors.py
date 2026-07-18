"""Tests for graph_neighbors.py — link resolution and boost calculation."""
from __future__ import annotations

import sys
import time
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


def test_resolve_path_wikilink_uses_the_one_existing_candidate(fake_graph):
    target = MagicMock()
    target.resolve.return_value = target
    target.exists.return_value = True
    target.is_file.return_value = True
    target.read_text.return_value = "# Active"
    target.relative_to.return_value.as_posix.return_value = "knowledge/notes/page.md"
    missing = MagicMock()
    missing.resolve.return_value = missing
    missing.exists.return_value = False
    with patch.object(fake_graph, "ROOT") as root:
        root.__truediv__.side_effect = [missing, target]
        assert fake_graph._resolve_wikilink("knowledge/notes/page.md") == (
            "knowledge/notes/page.md"
        )


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


def test_build_link_graph_extracts_wikilinks(fake_graph):
    """_build_link_graph finds [[wikilinks]] in markdown files."""
    fake_md = MagicMock()
    fake_md.is_file.return_value = True
    fake_md.read_text.return_value = "# Page A\n\nSee [[Page B]] and [[Page C]]."
    fake_md.relative_to.return_value.as_posix.return_value = "page_a.md"

    with patch.object(fake_graph, "KNOWLEDGE_DIR") as knowledge, \
         patch.object(fake_graph, "_resolve_wikilink", side_effect=lambda t, **_kwargs: f"{t.lower().replace(' ', '-')}.md"), \
         patch.object(Path, "rglob", return_value=[fake_md]):
        knowledge.exists.return_value = True
        knowledge.rglob.return_value = [fake_md]

        graph = fake_graph._build_link_graph()
        assert "page_a.md" in graph
        assert len(graph["page_a.md"]) == 2


def test_active_generation_is_preferred_and_source_scan_is_honest_fallback(fake_graph):
    active = {"knowledge/notes/a.md": ["knowledge/notes/b.md"]}
    with patch.object(fake_graph, "_read_active_link_graph", return_value=active), patch.object(
        fake_graph, "_build_link_graph"
    ) as source_scan:
        assert fake_graph.get_link_graph(catalog=object()) == active
        source_scan.assert_not_called()

    with patch.object(fake_graph, "_read_active_link_graph", return_value=None), patch.object(
        fake_graph, "_build_link_graph", return_value={"fallback.md": ["target.md"]}
    ):
        assert fake_graph.get_link_graph(catalog=object()) == {
            "fallback.md": ["target.md"]
        }


def test_neighbor_records_are_ordered_by_hop_then_node(fake_graph):
    fake_graph._link_graph_cache = {
        "a.md": ["c.md", "b.md"],
        "b.md": ["d.md"],
        "c.md": ["d.md"],
    }

    assert fake_graph.get_neighbor_records("a.md", max_hops=2) == [
        {"path": "b.md", "hop": 1},
        {"path": "c.md", "hop": 1},
        {"path": "d.md", "hop": 2},
    ]


def test_boost_ties_are_ordered_by_path(fake_graph):
    fake_graph._link_graph_cache = {"seed.md": ["z.md", "a.md"]}
    boosts = fake_graph.boost_graph_neighbors(
        [{"path": "seed.md"}], None, boost_weight=0.0
    )
    assert [item["path"] for item in boosts] == ["a.md", "z.md"]


def test_source_scan_fallback_honors_expired_deadline(fake_graph):
    with patch.object(fake_graph, "_read_active_link_graph", return_value=None):
        with pytest.raises(TimeoutError, match="deadline"):
            fake_graph.get_link_graph(
                catalog=object(), deadline=time.monotonic() - 1
            )
