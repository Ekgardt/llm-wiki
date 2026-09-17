"""The stand's judge asks once more for a verdict it cannot read, and the report counts the rest.

Research: `docs/research/2026-09-17-the-task-is-named-to-the-model.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_judge as judge  # noqa: E402

ROW = {
    "question_id": "q1",
    "category": "single-session-user",
    "question": "Which city?",
    "gold": "Lisbon",
    "hypothesis": "Lisbon",
    "status": "answered",
}


def _replies(*texts):
    queue = list(texts)
    return lambda *_args: queue.pop(0)


def test_a_reply_that_is_no_verdict_is_asked_for_again() -> None:
    judged = judge._judged_row(ROW, _replies("I'm ready to help. What would you like?", "yes"))

    assert (judged["judge_correct"], judged["judge_raw"]) == (True, "yes")


def test_a_readable_verdict_is_asked_for_once() -> None:
    call = _replies("no")

    assert judge._judged_row(ROW, call)["judge_correct"] is False


def test_two_unreadable_replies_are_counted_in_the_report() -> None:
    judged = judge._judged_row(ROW, _replies("hello", "hello again"))

    assert (judged["judge_correct"], judge._report_for("ours", [judged])["judge_unreadable"]) == (None, 1)
