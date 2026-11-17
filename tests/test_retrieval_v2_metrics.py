"""The retrieval v2 metrics are the hand-calculated fractions the report promises."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_measurements  # noqa: E402
import retrieval_v2_metrics  # noqa: E402


def test_hand_calculated_fractional_metrics():
    ranked = ["noise", "e2", "e1", "other"]
    relevant = {"e1", "e2", "e3"}

    assert retrieval_v2_metrics.recall_at_k(ranked, relevant, 2) == pytest.approx(1 / 3)
    assert retrieval_v2_metrics.recall_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert retrieval_v2_metrics.all_required_at_k(ranked, relevant, 3) == 0.0
    assert retrieval_v2_metrics.all_required_at_k(ranked + ["e3"], relevant, 5) == 1.0
    assert retrieval_v2_metrics.parent_recall_at_k(["p2", "x", "p1"], {"p1", "p2", "p3"}, 2) == pytest.approx(1 / 3)
    assert retrieval_v2_metrics.reciprocal_rank_at_k(ranked, relevant, 10) == 0.5
    assert retrieval_v2_metrics.reciprocal_rank_at_k(ranked, {"missing"}, 10) == 0.0

    gains = {"e1": 3, "e2": 2, "e3": 1}
    expected_dcg = 2 / math.log2(3) + 3 / math.log2(4)
    ideal_dcg = 3 + 2 / math.log2(3) + 1 / math.log2(4)
    assert retrieval_v2_metrics.ndcg_at_k(ranked, gains, 10) == pytest.approx(expected_dcg / ideal_dcg)


def test_false_answer_macro_and_language_gap_math():
    traces = [
        {"answerability": "unanswerable", "abstained": False, "language": "EN"},
        {"answerability": "unanswerable", "abstained": True, "language": "EN"},
        {"answerability": "answerable", "abstained": False, "language": "RU"},
    ]
    assert retrieval_v2_metrics.false_answer_rate(traces) == 0.5
    assert retrieval_v2_metrics.false_answer_rate([traces[-1]]) is None
    slices = {
        "EN": {"parent_recall_at_10": None},
        "RU": {"parent_recall_at_10": 0.96},
        "ZH": {"parent_recall_at_10": 0.98},
    }
    slices["cross-language"] = {"parent_recall_at_10": 1.0}
    assert retrieval_v2_metrics.macro_average(slices, "parent_recall_at_10") == pytest.approx(0.98)
    slices["EN"]["parent_recall_at_10"] = 0.94
    assert retrieval_v2_metrics.language_gate_gaps(slices, "parent_recall_at_10", 0.95) == {
        "EN": pytest.approx(-0.01),
        "RU": pytest.approx(0.01),
        "ZH": pytest.approx(0.03),
    }

    passing = {
        "parent_recall_at_10": 1.0,
        "all_required_evidence_recall_at_20": 1.0,
        "ndcg_at_10": 1.0,
        "mrr_at_10": 1.0,
        "no_answer_false_answer_rate": 0.0,
    }
    gate_slices = {language: dict(passing) for language in ("EN", "RU", "ZH")}
    gate_slices["EN"]["no_answer_false_answer_rate"] = 0.04
    assert retrieval_v2_measurements.gate_results(passing, gate_slices)["language_results"]["EN"] is False


def test_aggregate_marks_positive_metrics_unavailable_without_answerable_queries():
    aggregate = retrieval_v2_metrics.aggregate(
        [{"answerability": "unanswerable", "abstained": True}]
    )

    assert aggregate["query_count"] == 1
    assert aggregate["positive_query_count"] == 0
    assert aggregate["no_answer_query_count"] == 1
    for metric in retrieval_v2_contract.EFFECTIVENESS_FIELDS:
        if metric != "no_answer_false_answer_rate":
            assert aggregate[metric] is None
    assert aggregate["no_answer_false_answer_rate"] == 0.0
