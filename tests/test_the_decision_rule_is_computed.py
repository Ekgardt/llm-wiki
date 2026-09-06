"""The rule that decides a benchmark arm must be computed, not written by hand.

Six LongMemEval runs on this vault span 0.11 at n=50, and every published
figure the backlog compares against is a three-run mean. Yesterday's packing
change produced the highest of five runs and could not be called a win; that was
the right answer and an unaffordable one, because the largest item in the
backlog risks a regression on the categories that currently work and this stand
could not see one either.

These tests pin the rule from
`docs/research/2026-08-31-a-decision-rule-stated-before-the-run.md`: an arm wins
a category only by more than the baseline's own observed spread, a drop by more
than that spread is a loss that blocks the change, and everything else is "no
difference measured" — which is not "no difference" and is not a win.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import compare_arms  # noqa: E402


def _report(**categories: float) -> dict:
    return {name: {"accuracy": value, "n": 50} for name, value in categories.items()}


def _arm(*values: float) -> list[dict]:
    return [_report(overall=value) for value in values]


def _verdict(baseline: list[float], candidate: list[float]) -> str:
    rows = compare_arms.compare(_arm(*baseline), _arm(*candidate))
    return rows["overall"]["verdict"]


def test_a_gain_inside_the_baseline_spread_is_not_a_win() -> None:
    """Yesterday's case: the best of five runs, and still not evidence."""
    assert _verdict([0.29, 0.32, 0.35], [0.40, 0.36, 0.35]) == compare_arms.NO_DIFFERENCE


def test_a_gain_larger_than_the_spread_is_a_win() -> None:
    assert _verdict([0.30, 0.31, 0.32], [0.45, 0.46, 0.47]) == compare_arms.WIN


def test_a_drop_larger_than_the_spread_is_a_loss() -> None:
    assert _verdict([0.60, 0.61, 0.62], [0.40, 0.41, 0.42]) == compare_arms.LOSS


def test_a_loss_in_one_category_blocks_the_change() -> None:
    """The guard the turn-granularity item needs: overall up, one category down."""
    baseline = [
        _report(**{"single-session-user": 0.80, "multi-session": 0.10, "overall": 0.40}),
        _report(**{"single-session-user": 0.81, "multi-session": 0.11, "overall": 0.41}),
    ]
    candidate = [
        _report(**{"single-session-user": 0.50, "multi-session": 0.40, "overall": 0.45}),
        _report(**{"single-session-user": 0.51, "multi-session": 0.41, "overall": 0.46}),
    ]

    rows = compare_arms.compare(baseline, candidate)

    assert rows["multi-session"]["verdict"] == compare_arms.WIN
    assert rows["single-session-user"]["verdict"] == compare_arms.LOSS
    assert compare_arms._blocking_losses(rows) == ["single-session-user"]


def test_a_baseline_that_never_moved_still_needs_a_real_gain() -> None:
    """Zero spread must not turn any difference at all into a win."""
    assert _verdict([0.40, 0.40, 0.40], [0.40, 0.40, 0.40]) == compare_arms.NO_DIFFERENCE
    assert _verdict([0.40, 0.40, 0.40], [0.41, 0.41, 0.41]) == compare_arms.WIN


def test_the_summary_states_every_number_the_verdict_used() -> None:
    summary = compare_arms.summarise([0.30, 0.40, 0.35])

    assert summary == {
        "runs": 3,
        "mean": 0.35,
        "min": 0.3,
        "max": 0.4,
        "spread": 0.1,
    }


def test_a_missing_category_is_not_a_verdict() -> None:
    """An arm that never ran a category has not beaten anything in it."""
    rows = compare_arms.compare([_report(overall=0.4)], [_report(alpha=0.9)])

    assert rows["alpha"]["verdict"] == compare_arms.NO_DIFFERENCE
    assert rows["alpha"]["baseline"]["runs"] == 0


def test_overall_is_reported_last() -> None:
    """It is the summary line, and reading it first is how a loss gets missed."""
    rows = compare_arms.compare(
        [_report(**{"overall": 0.4, "temporal-reasoning": 0.1})],
        [_report(**{"overall": 0.5, "temporal-reasoning": 0.2})],
    )

    assert list(rows) == ["temporal-reasoning", "overall"]


def test_an_arm_with_too_few_runs_is_named_as_weak() -> None:
    """One run has no spread of its own; a verdict on it is a first look."""
    rows = compare_arms.compare(_arm(0.30, 0.31, 0.32), _arm(0.50))

    assert rows["overall"]["verdict"] == compare_arms.WIN
    assert compare_arms.under_run_arms(rows) == ["candidate"]


def test_three_runs_each_is_not_weak() -> None:
    rows = compare_arms.compare(_arm(0.30, 0.31, 0.32), _arm(0.50, 0.51, 0.52))

    assert compare_arms.under_run_arms(rows) == []


def test_the_comparison_round_trips_as_json() -> None:
    """The report is evidence; it has to survive being written down."""
    rows = compare_arms.compare(_arm(0.3, 0.4), _arm(0.5, 0.6))

    assert json.loads(json.dumps(rows)) == rows


def test_the_report_prices_a_right_answer_in_tokens() -> None:
    """Accuracy alone chose the widest window; economy alone would choose the
    weakest. Neither is the quantity that matters, and until 2026-09-06 nobody
    computed the one that is.

    On the 2026-09-03 sweep this reads 86.8, 52.7, 20.4 and 15.3 for answer
    budgets of 12 288, 32 768, 122 880 and 262 144 — and it reverses the reading
    taken from accuracy alone.
    """
    import longmemeval_score

    rows = [
        {"judge_correct": True, "est_total_prompt_tokens": 1_000_000},
        {"judge_correct": False, "est_total_prompt_tokens": 1_000_000},
    ]

    assert longmemeval_score._correct_per_million_tokens(rows) == 0.5


def test_a_ratio_without_a_judge_is_no_number() -> None:
    import longmemeval_score

    rows = [{"est_total_prompt_tokens": 1_000_000}]

    assert longmemeval_score._correct_per_million_tokens(rows) is None


def test_a_run_that_spent_nothing_prices_nothing() -> None:
    import longmemeval_score

    rows = [{"judge_correct": True, "est_total_prompt_tokens": 0}]

    assert longmemeval_score._correct_per_million_tokens(rows) is None
