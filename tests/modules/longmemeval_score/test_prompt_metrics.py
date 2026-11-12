"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_score.prompt_metrics_fixtures import (
    _MISSING_EVIDENCE,
    _SPAN_GOLD,
    _overall,
    _row,
)

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def test_the_rubric_row_leaves_the_gold_text_denominator() -> None:
    """Two of three questions have a gold a prompt could carry, and one did."""
    overall = _overall()

    assert (overall["gold_text_in_prompt"], overall["gold_text_applicable"]) == (1, 2)
    assert (overall["gold_text_not_applicable"], overall["gold_text_in_prompt_share"]) == (1, 0.5)


def test_the_evidence_figure_counts_every_type_including_the_rubric_one() -> None:
    """The dataset labels the answer-carrying turns, so this is always defined."""
    overall = _overall()

    assert (overall["evidence_in_prompt"], overall["evidence_measured"]) == (2, 3)
    assert overall["evidence_in_prompt_share"] == 0.6667


def test_a_question_whose_evidence_never_arrived_is_still_a_miss() -> None:
    """Holding out the undefined case must not soften the defined one."""
    missed = longmemeval_score.aggregate([_MISSING_EVIDENCE])["temporal-reasoning"]

    assert (missed["evidence_in_prompt"], missed["evidence_measured"]) == (0, 1)
    assert missed["evidence_in_prompt_share"] == 0.0


def test_a_run_that_never_recorded_the_evidence_reports_none_not_zero() -> None:
    """Runs before 2026-09-18 have no such field, and absence is not a failure."""
    old = longmemeval_score.aggregate([_row("multi-session", gold_in_prompt=True)])["multi-session"]

    assert (old["evidence_measured"], old["evidence_in_prompt"]) == (0, 0)
    assert old["evidence_in_prompt_share"] is None


def test_a_question_that_never_reached_a_prompt_is_in_no_denominator() -> None:
    """A harness failure built no prompt; counting it would invent a miss."""
    failed = _row("multi-session", status="error", error_kind="harness_failure")
    report = longmemeval_score.aggregate([_SPAN_GOLD, failed])["multi-session"]

    assert (report["n"], report["scored"], report["gold_text_applicable"]) == (2, 1, 1)
    assert report["gold_text_in_prompt_share"] == 1.0


def test_an_abstentions_explanation_is_no_more_a_span_than_a_rubric_is() -> None:
    """Its gold says why nothing can be answered; the run read 0 of 30 for that."""
    row = _row("abstention", is_abstention=True, gold_in_prompt=False)
    report = longmemeval_score.aggregate([row])["abstention"]

    assert (report["gold_text_applicable"], report["gold_text_not_applicable"]) == (0, 1)
    assert report["gold_text_in_prompt_share"] is None
