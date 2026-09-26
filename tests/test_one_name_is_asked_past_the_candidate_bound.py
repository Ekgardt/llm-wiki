"""A dead-code question about one name is answered past the candidate bound (audit 2026-09-26 B-7).

docs/research/2026-09-26-one-name-is-asked-past-the-candidate-bound.md
"""
from __future__ import annotations

from tests.test_whole_graph_aggregates import graph  # noqa: F401

QUERY = {
    "kinds": ("function", "method"),
    "incoming_edge_types": ("CALLS",),
    "outgoing_edge_types": ("EXPOSES",),
    "exclude_name_prefixes": ("test_",),
    "max_rows": 2,
}


def test_the_whole_scan_returns_its_first_rows_and_says_it_was_cut(graph) -> None:  # noqa: F811
    dead, truncated = graph.nodes_without_edges(**QUERY)

    assert (len(dead), truncated) == (2, True)


def test_one_name_is_found_in_sql_under_the_same_bound(graph) -> None:  # noqa: F811
    dead, truncated = graph.nodes_without_edges(**QUERY, name="stale")

    assert ([node["metadata"]["name"] for node in dead], truncated) == (["stale"], False)
