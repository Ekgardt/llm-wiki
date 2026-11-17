"""A name inside a question is a hint, not an order to search by spelling only.

Measured on LongMemEval-S (500 questions, 2026-11-17): 40 ordinary questions
were routed to the lexical-only EXACT profile because they held a hyphenated
word ("5-day trip", "gin-to-vermouth", "week-long"), and every one of them then
skipped the cross-encoder. Two callers made it worse: the answer path and the
benchmark passed the planner's *recommendation* back as if the caller had chosen
it, so EXACT and TEMPORAL questions ran lexical only and GRAPH ones lost the
dense leg. Only a query that is one name and nothing else is a lookup; a file
that has that name is still put first after reranking.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "scripts", ROOT / "benchmark"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

QUESTIONS_WITH_A_NAME = (
    "How many shirts did I pack for my 5-day trip?",
    "which modules use QueueWorker",
    "what did we decide about search_memory_rebuild?",
)


@pytest.mark.parametrize("question", QUESTIONS_WITH_A_NAME)
def test_a_question_holding_a_name_runs_hybrid(question: str) -> None:
    from retrieval import analyze_query

    analysis = analyze_query(question)

    assert "exact_identifier" in analysis.intents
    assert analysis.recommended_profile == "HYBRID"


@pytest.mark.parametrize(
    "query",
    ["hook-scripts-defense-in-depth", "auth-decision.md", "QueueWorker", "scripts/retrieval.py"],
)
def test_a_query_that_is_only_a_name_is_still_a_lookup(query: str) -> None:
    from retrieval import analyze_query

    assert analyze_query(query).recommended_profile == "EXACT"


def test_a_name_does_not_switch_the_reranker_off(monkeypatch) -> None:
    import reranker

    monkeypatch.setattr(reranker, "reranker_installed", lambda: True)
    docs = [{"rrf_score": 1.0}, {"rrf_score": 0.9}]

    decision = reranker.should_rerank(
        profile="HYBRID", candidates=docs, analysis_intents=("exact_identifier", "question")
    )

    assert decision == (True, None)


def test_the_answer_path_lets_retrieval_plan_the_legs(tmp_path: Path, monkeypatch) -> None:
    """The planner's recommendation is not the caller's choice of profile."""
    import retrieval
    from query_memory import grounded_qa

    asked: list[object] = []

    class Stop(Exception):
        pass

    def recording(query, **kwargs):
        asked.append(kwargs.get("profile"))
        raise Stop

    monkeypatch.setattr(retrieval, "retrieve_via_search_memory", recording)

    with pytest.raises(Stop):
        grounded_qa("decisions since 2025-01-01", vault=tmp_path, snapshot=object())

    assert asked == [None]


def test_the_benchmark_lets_retrieval_plan_the_legs(monkeypatch) -> None:
    import longmemeval_vault
    import retrieval

    asked: list[object] = []
    monkeypatch.setattr(
        retrieval,
        "retrieve_via_search_memory",
        lambda query, **kwargs: asked.append(kwargs.get("profile")) or [],
    )

    longmemeval_vault._retrieved_rows("what depends on search_memory")

    assert asked == [None]


@pytest.mark.parametrize(
    ("question", "profile"),
    [
        ("decisions since 2025-01-01", "HYBRID"),
        ("what depends on search_memory", "GRAPH"),
        ("How many shirts did I pack for my 5-day trip?", "HYBRID"),
    ],
)
def test_the_reported_profile_is_the_one_that_runs(question: str, profile: str) -> None:
    import longmemeval_vault
    from retrieval import planned_profile

    assert planned_profile(question) == profile
    assert longmemeval_vault.profile_for(question) == profile
