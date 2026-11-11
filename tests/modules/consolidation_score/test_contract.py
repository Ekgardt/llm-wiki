"""Original paired scoring assertions, including exact parameterized cases."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.modules.consolidation_score.fixtures import answered, failed, paired

BENCHMARK = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import consolidation_score  # noqa: E402


@pytest.mark.parametrize(
    ("baseline_only", "consolidated_only", "expected"),
    [
        (0, 0, 1.0),
        (0, 5, 0.0625),
        (1, 4, 0.375),
        (5, 5, 1.0),
        (0, 1, 1.0),
        (2, 2, 1.0),
    ],
)
def test_mcnemar_exact_matches_the_binomial_by_hand(
    baseline_only: int, consolidated_only: int, expected: float
) -> None:
    """Two-sided exact binomial over the discordant pairs, p = 0.5.

    5-0 is 2 * C(5,0) / 2^5 = 0.0625; 4-1 is 2 * (C(5,0)+C(5,1)) / 2^5 = 0.375;
    a single discordant pair cannot distinguish anything, so it is 1.0.
    """
    assert consolidation_score.mcnemar_exact(baseline_only, consolidated_only) == pytest.approx(
        expected
    )


def test_no_disagreement_is_no_evidence_not_an_error() -> None:
    rows = [paired(f"q{index}", answered("4200"), answered("4200")) for index in range(6)]
    report = consolidation_score.paired_report(rows)
    assert report["difference"]["accuracy_delta"] == 0.0
    assert report["difference"]["mcnemar_exact_p"] == 1.0
    assert report["difference"]["both_or_neither"] == 6


def _clean_win_difference() -> dict:
    rows = [
        paired(f"win{index}", answered("no idea"), answered("4200")) for index in range(5)
    ]
    return consolidation_score.paired_report(rows)["difference"]


def test_a_clean_win_for_consolidation_counts_only_the_winning_arm() -> None:
    difference = _clean_win_difference()
    assert difference["consolidated_only_correct"] == 5
    assert difference["baseline_only_correct"] == 0


def test_a_clean_win_for_consolidation_is_reported_with_its_p() -> None:
    difference = _clean_win_difference()
    assert difference["accuracy_delta"] == 1.0
    assert difference["mcnemar_exact_p"] == pytest.approx(0.0625)


def test_a_clean_loss_is_reported_with_the_same_p_and_the_opposite_sign() -> None:
    rows = [
        paired(f"lose{index}", answered("4200"), answered("no idea")) for index in range(5)
    ]
    difference = consolidation_score.paired_report(rows)["difference"]
    assert difference["baseline_only_correct"] == 5
    assert difference["accuracy_delta"] == -1.0
    assert difference["mcnemar_exact_p"] == pytest.approx(0.0625)


def test_a_provider_failure_on_either_arm_drops_the_pair() -> None:
    """Nothing was produced to compare, so the pair says nothing about memory."""
    rows = [
        paired("good", answered("4200"), answered("4200")),
        paired("left", failed(), answered("4200")),
        paired("right", answered("4200"), failed("provider_deadline")),
    ]
    report = consolidation_score.paired_report(rows)
    assert report["pairs_attempted"] == 3
    assert report["pairs_graded"] == 1
    assert report["pairs_dropped_ungraded"] == 2


def test_a_broken_arm_is_dropped_not_scored_as_wrong() -> None:
    """An activation refusal is a broken stand, not a wrong memory system."""
    rows = [
        paired("ok", answered("4200"), answered("4200")),
        paired("broke", answered("4200"), failed("arm_failure")),
        paired("worker", failed("harness_failure"), failed("harness_failure")),
    ]
    report = consolidation_score.paired_report(rows)
    assert report["pairs_graded"] == 1
    assert report["pairs_dropped_ungraded"] == 2


def test_our_own_refusal_is_graded_not_dropped() -> None:
    """`insufficient_evidence` is an outcome of the memory system, not a failure."""
    rows = [
        paired("a", {"status": "insufficient_evidence", "hypothesis": "", "error": None},
               answered("4200")),
    ]
    report = consolidation_score.paired_report(rows)
    assert report["pairs_graded"] == 1
    assert report["baseline"]["insufficient_evidence"] == 1
    assert report["difference"]["consolidated_only_correct"] == 1


def test_an_abstention_question_is_correct_only_when_the_arm_abstains() -> None:
    row = paired(
        "80ec1f4f_abs",
        {"status": "insufficient_evidence", "hypothesis": "", "error": None},
        answered("the campaign reached 4200 people"),
        is_abstention=True,
    )
    row["is_abstention"] = True
    report = consolidation_score.paired_report([row])
    assert report["baseline"]["accuracy"] == 1.0
    assert report["consolidated"]["accuracy"] == 0.0


def test_an_arm_row_carries_the_shared_question_fields_and_names_its_arm() -> None:
    row = consolidation_score.arm_row(
        paired("q1", answered("4200", retrieve_seconds=3.5), answered("nope")), "baseline"
    )
    shared = {key: row.get(key) for key in ("arm", "question_id", "gold", "retrieve_seconds")}
    assert shared == {
        "arm": "baseline",
        "question_id": "q1",
        "gold": "4200",
        "retrieve_seconds": 3.5,
    }
    assert row["question"] == "how many people did the campaign reach?"


def test_cost_means_come_from_the_consolidation_block() -> None:
    rows = [
        paired("q1", answered("a"), answered("b"),
               consolidation={"provider_calls": 10, "seconds": 100.0, "items": 4, "days": 11}),
        paired("q2", answered("a"), answered("b"),
               consolidation={"provider_calls": 12, "seconds": 200.0, "items": 6, "days": 11}),
    ]
    cost = consolidation_score.paired_report(rows)["consolidation_cost"]
    assert cost["mean_provider_calls"] == 11.0
    assert cost["mean_seconds"] == 150.0
    assert cost["mean_items"] == 5.0
    assert cost["mean_days"] == 11.0
