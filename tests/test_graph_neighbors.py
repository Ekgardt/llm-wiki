"""Tests for graph_neighbors.py — link resolution and boost calculation."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
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


def test_active_generation_invalidates_explicit_source_scan_cache(fake_graph):
    fake_graph._link_graph_cache = {"stale.md": ["fallback.md"]}
    active = {"current.md": ["generation.md"]}

    with patch.object(fake_graph, "_read_active_link_graph", return_value=active):
        assert fake_graph.get_link_graph(catalog=object()) == active
        assert fake_graph._link_graph_cache is None


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


@pytest.mark.parametrize("bind_generation", [True, False], ids=("other-repository", "legacy-unbound"))
def test_shared_state_generation_for_another_repository_uses_honest_fallback(
    fake_graph, tmp_path, monkeypatch, bind_generation
):
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    repository_a = tmp_path / "first" / "same-name"
    repository_b = tmp_path / "second" / "same-name"
    repository_a.mkdir(parents=True)
    repository_b.mkdir(parents=True)
    catalog = GenerationCatalog(tmp_path / "state")
    content = b"# A\n"
    build_full_generation(
        catalog,
        sources=(
            {
                "source_id": "a",
                "relative_path": "knowledge/notes/a.md",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "media_type": "text/markdown",
                "language": "en",
                "git_oid": None,
            },
        ),
        source_bytes={"a": content},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="repository-a",
        **(
            {"repository_scope": resolve_repository_scope(repository_a)}
            if bind_generation
            else {}
        ),
    )
    fallback = {"knowledge/notes/b.md": ["knowledge/notes/local.md"]}
    monkeypatch.setattr(fake_graph, "ROOT", repository_b)
    monkeypatch.setattr(fake_graph, "KNOWLEDGE_DIR", repository_b / "knowledge" / "notes")
    monkeypatch.setattr(fake_graph, "_build_link_graph", lambda **_kwargs: fallback)

    assert fake_graph.get_link_graph(catalog=catalog) == fallback


class _ActiveGraph:
    """An active generation whose LINKS_TO rows are given, and which records its close."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.closed = False

    def _execute(self, *_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


def _open_returns(monkeypatch: pytest.MonkeyPatch, graph: _ActiveGraph | None) -> None:
    import evidence_graph

    monkeypatch.setattr(
        evidence_graph.EvidenceGraph,
        "open_active_for_repository",
        classmethod(lambda _cls, *_args, **_kwargs: graph),
    )


def test_active_links_become_a_sorted_deduplicated_adjacency(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _ActiveGraph(
        [
            {"source_path": "b.md", "target_path": "c.md"},
            {"source_path": "a.md", "target_path": "z.md"},
            {"source_path": "a.md", "target_path": "y.md"},
            {"source_path": "a.md", "target_path": "z.md"},
        ]
    )
    _open_returns(monkeypatch, graph)

    assert fake_graph.get_link_graph(catalog=object()) == {
        "a.md": ["y.md", "z.md"],
        "b.md": ["c.md"],
    }
    assert graph.closed


def test_no_active_generation_scans_the_sources(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_returns(monkeypatch, None)
    fallback = {"scanned.md": ["target.md"]}
    monkeypatch.setattr(fake_graph, "_build_link_graph", lambda **_kwargs: fallback)

    assert fake_graph.get_link_graph(catalog=object()) == fallback


def test_an_unreadable_generation_scans_the_sources(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(fake_graph, "_query_links", broken)
    fallback = {"scanned.md": ["target.md"]}
    monkeypatch.setattr(fake_graph, "_build_link_graph", lambda **_kwargs: fallback)

    assert fake_graph.get_link_graph(catalog=object()) == fallback


def test_a_generation_read_past_its_deadline_is_not_hidden(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def late(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("graph deadline reached")

    monkeypatch.setattr(fake_graph, "_query_links", late)

    with pytest.raises(TimeoutError, match="deadline"):
        fake_graph.get_link_graph(catalog=object())


def test_a_state_root_without_a_catalog_has_no_active_graph(
    fake_graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_graph, "STATE_ROOT", tmp_path)

    assert fake_graph._read_active_link_graph(None) is None


def test_the_state_root_catalog_is_read_when_none_is_given(
    fake_graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from generation_catalog import GenerationCatalog

    catalog_path = tmp_path / "cache" / "evidence-graph" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    monkeypatch.setattr(fake_graph, "STATE_ROOT", tmp_path)

    assert isinstance(fake_graph._catalog_or_default(None), GenerationCatalog)


def _run_cli(
    fake_graph: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> str:
    links = {"a.md": ["b.md", "c.md"], "d.md": ["a.md"]}
    monkeypatch.setattr(fake_graph, "_link_graph_cache", links)
    monkeypatch.setattr(fake_graph, "_read_active_link_graph", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["graph_neighbors.py", *arguments])

    assert fake_graph.main() == 0
    return capsys.readouterr().out


def test_cli_stats_count_pages_and_edges(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run_cli(fake_graph, monkeypatch, capsys, "--stats")

    assert "Pages with outbound links: 2" in out
    assert "Total edges: 3" in out
    assert "a.md: 2 links" in out


def test_cli_neighbors_name_both_directions(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run_cli(fake_graph, monkeypatch, capsys, "--neighbors", "a.md")

    assert "→ b.md" in out
    assert "→ c.md" in out
    assert "← d.md" in out


def test_cli_without_a_request_prints_nothing(
    fake_graph: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(fake_graph, monkeypatch, capsys) == ""
