"""Every search runs every leg; the words of the query choose none of them.

Until 2026-11-17 word lists picked the profile: "depends on" added the graph,
"since 2025" made a question TEMPORAL and lexical only, and a reading that was
wrong was never recovered downstream. See
docs/research/2026-11-17-every-search-runs-every-leg.md.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import provenance  # noqa: E402
from retrieval import RetrievalResult, analyze_query, planned_request, retrieve  # noqa: E402

RELATION = "what depends on the capture queue"
EVERY_LEG = ("GLOBAL", ("lexical", "dense", "graph"))

# Each of these once read as a route of its own.
ONCE_ROUTED = (
    RELATION,
    "decisions since 2025-01-01",
    "show repo map of scripts",
    "impact of changing the queue",
    "synthesize architecture across all projects",
    "how does retry work after 2025",
    "capture queue retry",
    "\u4ec0\u4e48\u4f9d\u8d56\u4e8e\u6355\u83b7\u961f\u5217",
)


@pytest.mark.parametrize("query", ONCE_ROUTED)
def test_the_wording_of_a_query_plans_nothing(query: str) -> None:
    analysis = analyze_query(query)

    assert planned_request(None, semantic=True) == EVERY_LEG
    assert set(analysis.intents) <= {"quoted_phrase", "exact_identifier", "identifier_lookup"}


# The ones that hold no name: "2025-01-01" is read as one, as it was before.
NAMELESS = tuple(query for query in ONCE_ROUTED if not analyze_query(query).intents)


def test_most_once_routed_queries_hold_no_name() -> None:
    assert len(NAMELESS) == len(ONCE_ROUTED) - 1


@pytest.mark.parametrize("query", NAMELESS)
def test_the_wording_of_a_query_keeps_the_curated_prior(query: str) -> None:
    assert provenance.curated_pages_first(analyze_query(query).intents) is True


def test_a_named_symbol_still_reads_as_a_code_question() -> None:
    intents = analyze_query("what calls search_memory_rebuild").intents

    assert provenance.curated_pages_first(intents) is False


@pytest.mark.parametrize(
    ("profile", "semantic", "expected"),
    [
        ("EXACT", True, ("EXACT", ("lexical",))),
        ("IMPACT", True, ("IMPACT", ("lexical", "graph"))),
        (None, False, ("BASE", ("lexical",))),
    ],
)
def test_a_chosen_profile_runs_as_declared(
    profile: str | None, semantic: bool, expected: tuple[str, tuple[str, ...]]
) -> None:
    assert planned_request(profile, semantic=semantic) == expected


def test_a_caller_that_switched_the_graph_off_is_not_planned_one() -> None:
    """A plan asking for a leg its caller disabled would report it missing every time."""
    assert planned_request(None, semantic=True, graph=False) == (
        "HYBRID",
        ("lexical", "dense"),
    )
    assert planned_request("GRAPH", semantic=True, graph=False)[0] == "GRAPH"


def test_retrieve_with_the_graph_off_plans_no_graph() -> None:
    trace = retrieve(
        "capture queue retry",
        lexical_backend=lambda **_filters: [_hit("seed")],
        graph_enabled=False,
        rerank_enabled=False,
    ).trace

    assert trace.requested_mode == "HYBRID"
    assert trace.fallback_reason != "graph_disabled"


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


def _planned_run(query: str, graph: Callable[..., list[dict[str, object]]]) -> RetrievalResult:
    requested, signals = planned_request(None, semantic=True)
    return retrieve(
        query,
        requested_profile=requested,
        signals=signals,
        lexical_backend=lambda **_filters: [_hit("seed")],
        dense_backend=lambda **_filters: [_hit("near")],
        graph_backend=graph,
        rerank_enabled=False,
    )


@pytest.mark.parametrize("query", [RELATION, "capture queue retry"])
def test_a_planned_graph_run_uses_dense_and_graph(query: str) -> None:
    called: list[str] = []

    def graph(**_options):
        called.append("graph")
        return []

    trace = _planned_run(query, graph).trace

    assert (called, trace.requested_mode) == (["graph"], "GLOBAL")
    assert "dense" in trace.signals_used


@pytest.mark.parametrize("query", ONCE_ROUTED)
def test_retrieve_with_no_profile_plans_like_the_search(query: str) -> None:
    trace = retrieve(
        query,
        lexical_backend=lambda **_filters: [_hit("seed")],
        rerank_enabled=False,
    ).trace

    assert (trace.requested_mode, trace.signals_requested) == EVERY_LEG


def test_a_failed_graph_leaves_a_hybrid_answer() -> None:
    def graph(**_options):
        raise RuntimeError("graph store unreadable")

    trace = _planned_run(RELATION, graph).trace

    assert (trace.effective_mode, trace.signals_used) == ("HYBRID", ("lexical", "dense"))


def test_unknown_signals_are_refused() -> None:
    with pytest.raises(ValueError):
        retrieve("q", requested_profile="HYBRID", signals=("lexical", "telepathy"))


def test_recall_reports_a_signal_that_ran_beside_its_profile() -> None:
    """A run asked as GRAPH that also used dense reports dense, not only GRAPH's legs."""
    trace = {"requested_mode": "GRAPH", "signals_used": ["lexical", "dense", "graph"]}

    assert mcp_server._requested_signals(trace) == ("lexical", "graph", "dense")


def test_an_unreported_trace_names_the_mode_the_search_would_run() -> None:
    assert mcp_server._unreported_trace()["requested_mode"] == "GLOBAL"
