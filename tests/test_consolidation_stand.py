"""The MEM-13 consolidation stand measures what it claims to measure.

These tests exercise the stand itself, never the provider and never a vault:
the paired statistic, the arm-row shape the judge consumes, the rule that a
provider failure drops a pair instead of scoring it, the daily-file discovery
both arms share, and — the one that matters most — that the marker the stand
counts consolidation by still occurs in a block `episode_consolidation`
actually renders. A stand that silently stopped seeing consolidation output
would report "consolidation changes nothing" with total confidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import consolidation_score  # noqa: E402
import consolidation_vault  # noqa: E402
import run_consolidation  # noqa: E402


def paired(question_id: str, baseline: dict, consolidated: dict, **extra) -> dict:
    """A paired row in the shape the worker writes."""
    return {
        "question_id": question_id,
        "question_type": "multi-session",
        "category": "multi-session",
        "is_abstention": False,
        "question": "how many people did the campaign reach?",
        "gold": "4200",
        "baseline": baseline,
        "consolidated": consolidated,
        **extra,
    }


def answered(text: str, **extra) -> dict:
    return {"status": "answered", "hypothesis": text, "error": None, **extra}


def failed(kind: str = "provider_no_response") -> dict:
    return {"status": "error", "hypothesis": "", "error": "boom", "error_kind": kind}


# --- the paired statistic -------------------------------------------------


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


# --- what counts as a graded pair ----------------------------------------


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


# --- the arm rows the judge reads ----------------------------------------


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


def test_the_arm_streams_are_one_line_per_question_per_arm(tmp_path: Path) -> None:
    rows = [paired("q1", answered("4200"), answered("nope")),
            paired("q2", answered("nope"), answered("4200"))]
    results = tmp_path / "consolidation-run.jsonl"
    written = run_consolidation.write_arm_streams(rows, results)
    assert [path.name for path in written] == [
        "consolidation-run-baseline.jsonl",
        "consolidation-run-consolidated.jsonl",
    ]
    for path in written:
        assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_a_row_the_stand_died_on_is_not_treated_as_done() -> None:
    """An OOM-killed worker must not silently exclude its question from a resume."""
    died = paired("oom", failed("harness_failure"), failed("harness_failure"))
    measured = paired("ok", answered("4200"), answered("nope"))
    refused = paired(
        "refused",
        {"status": "insufficient_evidence", "hypothesis": "", "error": None},
        {"status": "insufficient_evidence", "hypothesis": "", "error": None},
    )
    assert run_consolidation._is_measured(died) is False
    assert run_consolidation._is_measured(measured) is True
    assert run_consolidation._is_measured(refused) is True


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


# --- what the worker measures --------------------------------------------


def test_the_marker_the_stand_counts_by_is_still_the_one_the_product_writes() -> None:
    """The guard against measuring nothing and calling it a null result."""
    assert consolidation_vault.marker_is_live() is True


def test_both_arms_discover_daily_files_from_disk(tmp_path: Path) -> None:
    """Consolidation writes into today's file, which the ingest list never names."""
    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    (daily / "2023-05-20.md").write_text("session\n", encoding="utf-8")
    (daily / "2026-08-28.md").write_text("consolidated\n", encoding="utf-8")
    (daily / "notes.txt").write_text("ignored\n", encoding="utf-8")
    assert consolidation_vault.daily_relative_paths(tmp_path) == [
        "knowledge/daily/2023-05-20.md",
        "knowledge/daily/2026-08-28.md",
    ]


def test_a_vault_with_no_daily_directory_yields_no_paths(tmp_path: Path) -> None:
    assert consolidation_vault.daily_relative_paths(tmp_path) == []


def test_retrieved_rows_carrying_consolidation_text_are_counted() -> None:
    marker = consolidation_vault.CONSOLIDATION_MARKER
    rows = [
        {"path": "knowledge/daily/2026-08-28.md", "content": f"- `[03:00:01{marker}2023-05-20`"},
        {"path": "knowledge/daily/2023-05-20.md", "content": "a raw captured session"},
    ]
    assert consolidation_vault.rows_from_consolidation(rows) == 1
    assert consolidation_vault.rows_from_consolidation([]) == 0


def test_marker_hits_counts_every_occurrence_in_a_prompt() -> None:
    marker = consolidation_vault.CONSOLIDATION_MARKER
    assert consolidation_vault.marker_hits(f"x{marker}y{marker}z") == 2
    assert consolidation_vault.marker_hits("nothing here") == 0


# --- the sample ----------------------------------------------------------


def _question(index: int, question_type: str) -> dict:
    return {
        "question_id": f"{question_type}-{index:03d}",
        "question_type": question_type,
        "question": "?",
        "answer": "!",
    }


def test_the_slice_keeps_only_the_named_question_type() -> None:
    data = [_question(index, "multi-session") for index in range(5)]
    data += [_question(index, "temporal-reasoning") for index in range(5)]
    assert len(run_consolidation.slice_of(data, "multi-session")) == 5
    assert len(run_consolidation.slice_of(data, "all")) == 10


def test_the_same_flags_name_the_same_questions() -> None:
    data = [_question(index, "multi-session") for index in range(40)]

    class _Args:
        sample = 6
        seed = 13
        type = "multi-session"

    first = run_consolidation.sample_questions(data, _Args())
    second = run_consolidation.sample_questions(data, _Args())
    assert [row["question_id"] for row in first] == [row["question_id"] for row in second]
    assert len(first) == 6
