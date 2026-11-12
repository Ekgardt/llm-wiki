"""Runner and vault integration contracts for the MEM-13 consolidation stand.

Pure paired-statistic cases live in tests/modules/consolidation_score. These
retained assertions cover stream publication, resume, daily-file discovery,
real consolidation markers and deterministic runner selection.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.consolidation_score.fixtures import answered, failed, paired

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import consolidation_vault  # noqa: E402
import run_consolidation  # noqa: E402

# --- the arm rows the judge reads ----------------------------------------


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
