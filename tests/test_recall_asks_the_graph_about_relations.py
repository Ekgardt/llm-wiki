"""A question about relations asks the evidence graph, and keeps dense search.

Every semantic search ran HYBRID whatever the planner read, so the evidence graph
never answered `recall`, while grounded recall and the benchmark followed the
planner. See docs/research/2026-09-25-recall-asks-the-graph-about-relations.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
from retrieval import analyze_query, planned_request, retrieve  # noqa: E402

RELATION = "what depends on the capture queue"


def _plan(query: str, profile: str | None = None, *, semantic: bool = True):
    return planned_request(profile, analyze_query(query), semantic=semantic)


@pytest.mark.parametrize(
    ("query", "profile", "semantic", "expected"),
    [
        (RELATION, None, True, ("GRAPH", ("lexical", "dense", "graph"))),
        ("capture queue retry", None, True, ("HYBRID", ("lexical", "dense"))),
        (RELATION, "EXACT", True, ("EXACT", ("lexical",))),
        (RELATION, None, False, ("BASE", ("lexical",))),
    ],
)
def test_the_plan_adds_the_graph_only_for_relations(query, profile, semantic, expected) -> None:
    assert _plan(query, profile, semantic=semantic) == expected


def _hit(candidate_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "parent_id": f"{candidate_id}.md",
        "relative_path": f"{candidate_id}.md",
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": 1.0,
        "title": candidate_id,
        "content": "capture queue",
        "retrieval_confidence": "high",
    }


def _planned_run(graph):
    requested, signals = _plan(RELATION)
    return retrieve(
        RELATION,
        requested_profile=requested,
        signals=signals,
        lexical_backend=lambda **_filters: [_hit("seed")],
        dense_backend=lambda **_filters: [_hit("near")],
        graph_backend=graph,
        rerank_enabled=False,
    )


def test_a_planned_graph_run_uses_dense_and_graph() -> None:
    called: list[str] = []

    def graph(**_options):
        called.append("graph")
        return []

    trace = _planned_run(graph).trace

    assert (called, trace.requested_mode) == (["graph"], "GRAPH")
    assert "dense" in trace.signals_used


def test_a_failed_graph_leaves_a_hybrid_answer() -> None:
    def graph(**_options):
        raise RuntimeError("graph store unreadable")

    trace = _planned_run(graph).trace

    assert (trace.effective_mode, trace.signals_used) == ("HYBRID", ("lexical", "dense"))


def test_unknown_signals_are_refused() -> None:
    with pytest.raises(ValueError):
        retrieve("q", requested_profile="HYBRID", signals=("lexical", "telepathy"))


def test_recall_reports_the_dense_component_a_planned_graph_run_used() -> None:
    trace = {"requested_mode": "GRAPH", "signals_used": ["lexical", "dense", "graph"]}

    assert mcp_server._requested_signals(trace) == ("lexical", "graph", "dense")


def test_an_unreported_trace_names_the_mode_the_search_would_run() -> None:
    assert mcp_server._unreported_trace("capture queue retry")["requested_mode"] == "HYBRID"
