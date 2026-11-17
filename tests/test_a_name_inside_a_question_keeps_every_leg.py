"""A name inside a question is a hint, not an order to search by spelling only.

Measured on LongMemEval-S (500 questions, 2026-11-17): 40 ordinary questions
were routed to the lexical-only EXACT profile because they held a hyphenated
word ("5-day trip", "gin-to-vermouth", "week-long"), and every one of them then
skipped the cross-encoder. Two callers made it worse: the answer path and the
benchmark passed the planner's *recommendation* back as if the caller had chosen
it, so EXACT and TEMPORAL questions ran lexical only and GRAPH ones lost the
dense leg. Only a query made of names and their decoration is a lookup, and a
lookup keeps what it had: both legs, no cross-encoder, its lexical order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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


LOOKUPS = (
    "hook-scripts-defense-in-depth",
    "auth-decision.md",
    "QueueWorker",
    "search_memory_rebuild",
    "scripts/retrieval.py",
    "`QueueWorker`",
    "QueueWorker.run",
    "search_memory_rebuild()",
    "queue_worker.py:120",
)


def _hit(candidate_id: str, path: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "parent_id": path,
        "relative_path": path,
        "heading_path": (),
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": 1.0,
        "type": "concept",
        "content": f"text of {candidate_id}",
    }


def _retrieved_with_a_reversing_reranker(
    query: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, list[str]]:
    """Run the product's planning and reranking; the cross-encoder reverses the pool."""
    import reranker
    import retrieval

    scored: list[str] = []

    def reversing(question: str, rows: list[dict[str, Any]], **_: Any) -> list[dict[str, Any]]:
        scored.append(question)
        return [
            {**row, "rerank_score": float(place), "reranker_applied": True}
            for place, row in enumerate(rows)
        ][::-1]

    monkeypatch.setattr(reranker, "reranker_installed", lambda: True)
    monkeypatch.setattr(reranker, "rerank", reversing)
    hits = [
        _hit("c-named", "knowledge/notes/queue-notes.md"),
        _hit("c-other", "knowledge/notes/x.md"),
    ]
    profile = retrieval.planned_profile(query)
    result = retrieval.retrieve(
        query,
        requested_profile=profile,
        limit=2,
        lexical_backend=lambda **_kwargs: hits,
        dense_backend=lambda **_kwargs: hits,
        rerank_enabled=True,
        corpus_generation="gen-lookup",
    )
    return result, scored


@pytest.mark.parametrize("query", LOOKUPS)
def test_a_lookup_keeps_its_lexical_order_and_skips_the_cross_encoder(
    query: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, scored = _retrieved_with_a_reversing_reranker(query, monkeypatch)

    assert scored == []
    assert result.trace.reranker_fallback_reason == "exact_match_bypass"
    assert result.candidates[0].candidate_id == "c-named"


@pytest.mark.parametrize("question", QUESTIONS_WITH_A_NAME)
def test_a_question_holding_a_name_goes_through_the_cross_encoder(
    question: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, scored = _retrieved_with_a_reversing_reranker(question, monkeypatch)

    assert scored == [question]
    assert result.trace.reranker_applied is True


def test_a_name_does_not_switch_the_reranker_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import reranker

    monkeypatch.setattr(reranker, "reranker_installed", lambda: True)
    docs = [{"rrf_score": 1.0}, {"rrf_score": 0.9}]

    decision = reranker.should_rerank(
        profile="HYBRID", candidates=docs, analysis_intents=("exact_identifier", "question")
    )

    assert decision == (True, None)


_REFUSAL = json.dumps(
    {
        "schema_version": "grounded-answer/v1",
        "status": "insufficient_evidence",
        "claims": [],
        "citations": [],
        "reason": "nothing",
    }
)


def _empty_snapshot() -> object:
    from corpus_snapshot import CorpusSnapshot, SnapshotPolicy

    policy = SnapshotPolicy((), (), False, None, 100, 1024 * 1024, 1024 * 1024, 100, 100, 8)
    return CorpusSnapshot((), (), "0" * 64, policy)


def test_the_answer_path_lets_retrieval_plan_the_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner's recommendation is not the caller's choice of profile.

    Every search the answer makes is checked, the dated leg's included: a
    question that names a time searches again inside those days.
    """
    import retrieval
    from query_memory import grounded_qa

    asked: list[tuple[str, object, object]] = []

    def recording(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        asked.append((query, kwargs.get("profile"), kwargs.get("since")))
        return []

    monkeypatch.setattr(retrieval, "retrieve_via_search_memory", recording)

    grounded_qa(
        "Which book did I finish a week ago?\n(Current date: 2023/05/30 (Tue) 10:00)",
        vault=tmp_path,
        snapshot=_empty_snapshot(),
        generator=lambda *_: _REFUSAL,
    )

    assert len(asked) >= 2, asked
    assert any(since is not None for _, _, since in asked), asked
    assert [profile for _, profile, _ in asked] == [None] * len(asked)


def _record_retrieval(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Every query retrieval is asked, with the profile it was handed."""
    import retrieval

    asked: list[tuple[str, object]] = []

    def recording(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        asked.append((query, kwargs.get("profile")))
        return []

    monkeypatch.setattr(retrieval, "retrieve_via_search_memory", recording)
    return asked


def test_the_benchmark_lets_retrieval_plan_the_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    import longmemeval_vault

    asked = _record_retrieval(monkeypatch)

    longmemeval_vault._retrieved_rows("what depends on search_memory")

    assert asked == [("what depends on search_memory", None)]


QUESTION = {
    "question_id": "q1",
    "question_type": "multi-session",
    "question": "How many shirts did I pack for my 5-day trip?",
    "question_date": "2023/05/20 (Sat) 02:21",
    "answer": "7",
    "haystack_sessions": [],
    "answer_session_ids": [],
}


def test_a_benchmark_question_is_retrieved_unplanned_and_reported_as_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import longmemeval_vault as harness

    asked = _record_retrieval(monkeypatch)
    monkeypatch.setattr(harness, "prepare_environment", lambda work: (tmp_path, tmp_path))
    monkeypatch.setattr(harness, "_adopt", lambda root, state: None)
    monkeypatch.setattr(harness, "ingest_sessions", lambda root, question: ([], 0))
    monkeypatch.setattr(harness, "build_generation", lambda *args: (object(), {}))
    monkeypatch.setattr(harness, "_warm_reranker", lambda: None)
    monkeypatch.setattr(harness, "_answer_or_coverage", lambda *args: {})
    monkeypatch.setattr(harness, "_measured_compile", lambda *args: {})

    record = harness.run_question(QUESTION, tmp_path)

    assert [profile for _, profile in asked] == [None]
    assert record["profile"] == "HYBRID"


def test_retrieval_only_coverage_reads_the_dated_question_without_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import longmemeval_coverage
    import longmemeval_vault as harness

    asked = _record_retrieval(monkeypatch)
    monkeypatch.setenv(harness.RETRIEVAL_ONLY_VARIABLE, "1")
    monkeypatch.setattr(longmemeval_coverage, "coverage_at_depths", lambda question, rows: {})
    searchable = "How many shirts did I pack for my 5-day trip? 2023-05-20"

    outcome = harness._answer_or_coverage(QUESTION, tmp_path, None, [], {}, "HYBRID", searchable)

    assert outcome["status"] == "retrieval_only"
    assert asked == [(searchable, None)]


def test_a_consolidation_arm_lets_retrieval_plan_the_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consolidation_vault

    asked = _record_retrieval(monkeypatch)
    monkeypatch.setattr(consolidation_vault, "build_generation", lambda *args: (object(), {}))
    monkeypatch.setattr(consolidation_vault, "daily_relative_paths", lambda root: [])
    monkeypatch.setattr(consolidation_vault, "_answered", lambda *args: {})

    record = consolidation_vault.run_arm(QUESTION, tmp_path, tmp_path)

    assert asked == [(QUESTION["question"], None)]
    assert record["profile"] == "HYBRID"


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
