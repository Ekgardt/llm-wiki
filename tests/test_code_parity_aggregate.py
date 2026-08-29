"""The parity aggregator must report a spread, never collapse it to a mean.

CODE-07 re-run (2026-08-29). codebase-memory-mcp is non-deterministic on an
unchanged repository: its architecture summary graded correct in four of
eight runs and wrong in the other four. A mean would hide that; the whole
point of the aggregator is that a reader sees the range and can tell a
product difference from one product's own variance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

import aggregate_code_parity as agg  # noqa: E402


def _summary(correct: int, tokens: int) -> dict:
    return {
        "correct": correct,
        "partial": 0,
        "wrong": 13 - correct,
        "total_tokens": tokens,
        "total_seconds": 10.0,
        "wrong_but_confident": 13 - correct,
        "operator_attention_events": 0,
    }


def _task(task_id: str, best_grade: str, cbm_grade: str) -> dict:
    return {
        "id": task_id,
        "llm_wiki_best": {"grade": best_grade, "tokens": 300},
        "cbm": {"grade": cbm_grade, "tokens": 100},
    }


def _report(correct: int, tokens: int, tasks: list[dict]) -> dict:
    return {
        "summary": {
            "llm_wiki_best": _summary(11, 36382),
            "cbm": _summary(correct, tokens),
        },
        "tasks": tasks,
    }


def _write(tmp_path: Path, name: str, report: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_a_column_that_moved_between_runs_is_reported_as_a_range():
    values = [10, 11, 10]
    assert agg._spread(values) == "10-11"


def test_a_column_that_never_moved_is_reported_as_one_number():
    assert agg._spread([87527, 87527, 87527]) == "87527"


def test_the_side_row_carries_the_range_for_every_reported_column(tmp_path):
    reports = [
        _report(10, 2406, [_task("T01", "correct", "correct")]),
        _report(11, 2442, [_task("T01", "correct", "correct")]),
    ]
    row = agg._side_row(reports, "cbm")
    assert row["runs"] == 2
    assert row["correct"] == "10-11"
    assert row["total_tokens"] == "2406-2442"


def test_paired_tokens_uses_only_tasks_both_sides_got_right_in_that_run():
    report = _report(
        10,
        2406,
        [
            _task("T01", "correct", "correct"),
            _task("T12", "correct", "wrong"),
            _task("T13", "wrong", "wrong"),
        ],
    )
    entry = agg._paired_tokens_one(report, agg.PAIR)
    assert entry == {"tasks": 1, "llm_wiki_best": 300, "cbm": 100}


def test_a_run_where_the_other_side_answered_enters_the_paired_set():
    report = _report(
        11,
        2442,
        [_task("T01", "correct", "correct"), _task("T12", "correct", "correct")],
    )
    entry = agg._paired_tokens_one(report, agg.PAIR)
    assert entry["tasks"] == 2
    assert agg._ratio(entry, agg.PAIR) == 3.0


def test_a_side_missing_from_one_report_is_left_out_of_the_table(tmp_path):
    full = _report(10, 2406, [_task("T01", "correct", "correct")])
    partial = {
        "summary": {"llm_wiki_best": _summary(11, 36382)},
        "tasks": [_task("T01", "correct", "correct")],
    }
    assert agg._present_sides([full, partial]) == ["llm_wiki_best"]


def test_main_prints_the_range_and_the_paired_ratio(tmp_path, capsys):
    first = _write(
        tmp_path,
        "a.json",
        _report(10, 2406, [_task("T01", "correct", "correct")]),
    )
    second = _write(
        tmp_path,
        "b.json",
        _report(11, 2442, [_task("T01", "correct", "correct")]),
    )
    assert agg.main([str(first), str(second)]) == 0
    printed = capsys.readouterr().out
    assert "10-11" in printed
    assert "ratio=3.0x" in printed


def test_an_empty_report_list_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        agg.main([])
