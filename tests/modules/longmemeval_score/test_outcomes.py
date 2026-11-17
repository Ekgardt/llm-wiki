"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def test_an_abstention_with_the_answer_retrieved_is_a_calibration_failure() -> None:
    rows = [
        {
            "status": "insufficient_evidence",
            "answer_sessions_retrieved": 2,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained"] == 1
    assert report["abstained_with_answer_retrieved"] == 1
    assert report["abstained_without_answer_retrieved"] == 0


def test_an_abstention_with_nothing_retrieved_is_retrieval_refusing() -> None:
    rows = [
        {
            "status": "insufficient_evidence",
            "answer_sessions_retrieved": 0,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained_with_answer_retrieved"] == 0
    assert report["abstained_without_answer_retrieved"] == 1


def test_an_answered_question_is_not_an_abstention() -> None:
    rows = [
        {
            "status": "answered",
            "answer_sessions_retrieved": 1,
            "gold": "x",
            "hypothesis": "the answer is x",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained"] == 0
    assert report["answer_retrieved"] == 1


def test_a_provider_failure_is_not_counted_as_an_abstention() -> None:
    """Nothing was produced, so nothing declined; it is already held out."""
    rows = [
        {
            "status": "error",
            "error": "boom",
            "error_kind": "provider_no_response",
            "answer_sessions_retrieved": 1,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["provider_failures"] == 1
    assert report["abstained"] == 0


def test_a_harness_failure_is_not_a_wrong_answer() -> None:
    """A run that never reached the product reported `accuracy 0.0` for all 50.

    Measured 2026-08-29: a `--workdir` that did not exist failed every question
    in about two seconds on a `mkdtemp` traceback, and the report read
    `accuracy 0.0` with `provider_failures 0` in every category — a harness
    that never started, presented as a memory system that answered everything
    wrongly.
    """
    rows = [
        {
            "status": "error",
            "error": "worker rc=1",
            "error_kind": "harness_failure",
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["scored"] == 0
    assert report["accuracy"] is None
    assert report["harness_failures"] == 1
    assert report["provider_failures"] == 0


def test_a_provider_failure_and_a_harness_failure_are_counted_apart() -> None:
    """One says the provider did not answer; the other says nothing was asked."""
    rows = [
        {"error_kind": "harness_failure", "status": "error", "error": "e", "gold": "x"},
        {
            "error_kind": "provider_no_response",
            "status": "error",
            "error": "e",
            "gold": "x",
        },
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["harness_failures"] == 1
    assert report["provider_failures"] == 1
    assert report["scored"] == 0
