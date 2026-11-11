"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_score.rubrics_fixtures import _RUBRIC, _SPAN, _TEXT_KEYS

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def _metrics(row: dict) -> tuple:
    scored = longmemeval_score.score_question(row)
    return tuple(scored[key] for key in _TEXT_KEYS)


def test_a_rubric_gold_claims_none_of_the_text_metrics() -> None:
    assert _metrics(_RUBRIC) == (None, None, None, None)


def test_a_span_gold_is_still_scored_by_its_text() -> None:
    """The change is one category wide; a real gold keeps every metric."""
    assert _metrics(_SPAN) == (False, True, 0.5, True)


def test_the_rubric_row_leaves_the_accuracy_denominator() -> None:
    """A question the metric cannot read is not a question it answered wrongly."""
    report = longmemeval_score.aggregate([_RUBRIC, _SPAN])
    rubric = report["single-session-preference"]

    assert (rubric["n"], rubric["text_scored"], rubric["text_not_applicable"]) == (1, 0, 1)
    assert rubric["accuracy"] is None


def test_the_overall_row_counts_the_rows_it_could_read() -> None:
    overall = longmemeval_score.aggregate([_RUBRIC, _SPAN])["overall"]

    assert (overall["n"], overall["text_scored"], overall["text_not_applicable"]) == (2, 1, 1)
    assert overall["accuracy"] == 1.0


def test_an_abstention_is_never_read_as_a_rubric() -> None:
    """Its category is its own, and it is graded on whether the system refused."""
    abstention = {**_RUBRIC, "category": "abstention", "is_abstention": True}

    assert longmemeval_score.gold_is_rubric(abstention) is False
    assert longmemeval_score.score_question(abstention)["correct"] is False
