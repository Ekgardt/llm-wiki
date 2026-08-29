"""Two lists in the summary were unbounded and said nothing about it.

`entry_points` and `routes` were fetched at `max_rows=10_000` and returned
whole, with no count and no truncation flag, while every list beside them —
hotspots, communities — states a limit and whether it clipped. A reader could
not tell a complete listing from a clipped one, and the listing grew with the
repository.

Measured on this repository 2026-08-29: 97 entry points, every one a script's
`main`, cost 7 367 of the summary's 24 430 characters. Bounded and counted,
the whole summary falls from 6 107 tokens to 4 910.

Bounding the hotspot ranking too then cost a grade, and the reason is the
finding: the summary answered only the second half of "what are its main
modules and entry points". It named `scripts/mcp_server.py` — the documented
agent surface — solely because one of its functions ranked between 31st and
100th by incoming callers. No entry point names it, because it has no `main`.
A module ranking answers the first half directly and puts it at rank 30 or
better; with it the summary costs 2 835 tokens and the grade is correct again.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402


def _rows(count: int) -> list[dict]:
    return [{"file": f"f{index}.py", "line": index} for index in range(count)]


def test_a_long_listing_is_cut_and_says_so() -> None:
    fields = code_graph._listing_fields(_rows(97), "entry_points", 30)

    assert len(fields["entry_points"]) == code_graph.SUMMARY_LISTING_LIMIT
    assert fields["entry_points_count"] == 97
    assert fields["entry_points_truncated"] is True
    assert fields["entry_points_limit"] == code_graph.SUMMARY_LISTING_LIMIT


def test_a_short_listing_is_whole_and_says_so() -> None:
    fields = code_graph._listing_fields(_rows(3), "routes", 30)

    assert fields["routes"] == _rows(3)
    assert fields["routes_count"] == 3
    assert fields["routes_truncated"] is False


def test_an_empty_listing_still_carries_its_count() -> None:
    """Zero routes is an answer; an absent count is not."""
    fields = code_graph._listing_fields([], "routes", 30)

    assert fields["routes"] == []
    assert fields["routes_count"] == 0
    assert fields["routes_truncated"] is False


def test_the_cut_keeps_the_head_of_the_listing() -> None:
    """Order carries meaning here, so the bound takes from the tail."""
    fields = code_graph._listing_fields(_rows(50), "entry_points", 30)

    assert fields["entry_points"] == _rows(50)[: code_graph.SUMMARY_LISTING_LIMIT]


def test_the_bound_is_the_summary_s_own_number() -> None:
    """One rule for every list in the summary, not a fourth invented number."""
    assert code_graph.SUMMARY_LISTING_LIMIT == code_graph.COMMUNITY_LIMIT


def test_the_caller_can_ask_for_the_whole_ranking() -> None:
    """The objection to cutting a ranking was that nothing restored it."""
    assert code_graph.summary_listing_limit(None) == code_graph.SUMMARY_LISTING_LIMIT
    assert code_graph.summary_listing_limit(10) == 10
    assert code_graph.summary_listing_limit(code_graph.HOTSPOT_LIMIT) == (
        code_graph.HOTSPOT_LIMIT
    )


def test_a_limit_beyond_what_the_query_fetches_is_clamped() -> None:
    """Asking for more than the store looked at cannot invent rows."""
    assert code_graph.summary_listing_limit(10_000) == code_graph.HOTSPOT_LIMIT
    assert code_graph.summary_listing_limit(0) == 1
    assert code_graph.summary_listing_limit(-3) == 1


def test_the_hotspot_ranking_states_no_count_it_cannot_know() -> None:
    """Ten thousand members, a hundred fetched: any count would be the fetch."""
    fields = code_graph._hotspot_fields(_rows(100), True, 30)

    assert "hotspot_count" not in fields
    assert fields["hotspots_truncated"] is True
    assert len(fields["hotspots"]) == 30


def test_an_unclipped_ranking_says_it_was_not_clipped() -> None:
    fields = code_graph._hotspot_fields(_rows(5), False, 30)

    assert fields["hotspots_truncated"] is False


class _FakeGraph:
    """The two calls `_stored_modules` makes, and nothing else."""

    def __init__(self, counts: list[dict], truncated: bool, nodes: dict) -> None:
        self._counts = counts
        self._truncated = truncated
        self._nodes = nodes
        self.asked: dict = {}

    def top_incoming_edge_counts(self, **kwargs):
        self.asked = kwargs
        return self._counts, self._truncated

    def node(self, node_id: str):
        return self._nodes.get(node_id)


def _module_graph(truncated: bool = False) -> _FakeGraph:
    counts = [
        {"node_id": "n1", "incoming": 129},
        {"node_id": "n2", "incoming": 74},
    ]
    nodes = {
        "n1": {"metadata": {"name": "scripts.reliable_memory", "path": "a.py"}},
        "n2": {"metadata": {"name": "scripts.mcp_server", "path": "b.py"}},
    }
    return _FakeGraph(counts, truncated, nodes)


def test_the_module_ranking_asks_the_graph_about_imports() -> None:
    graph = _module_graph()

    code_graph._stored_modules(graph, 30)

    assert graph.asked == {
        "edge_types": ("IMPORTS",),
        "kinds": ("module",),
        "max_rows": 30,
    }


def test_the_summary_ranks_modules_by_who_imports_them() -> None:
    graph = _module_graph()

    modules, truncated = code_graph._stored_modules(graph, 30)

    assert truncated is False
    assert modules[0] == {
        "name": "scripts.reliable_memory",
        "path": "a.py",
        "importing_modules": 129,
    }


def test_a_ranked_module_whose_node_vanished_is_dropped() -> None:
    """A stale count must not become a row with no name and no path."""
    graph = _module_graph()
    graph._nodes.pop("n2")

    modules, _ = code_graph._stored_modules(graph, 30)

    assert code_graph._module_fields(modules, False, 30)["modules"] == [
        {"name": "scripts.reliable_memory", "path": "a.py", "importing_modules": 129}
    ]


def test_the_module_ranking_states_its_bound() -> None:
    fields = code_graph._module_fields([{"name": "x"}], True, 30)

    assert fields["modules_limit"] == 30
    assert fields["modules_truncated"] is True
