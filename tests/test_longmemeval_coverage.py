"""Coverage counts the evidence a question needs, not the rows that happen to match.

Research: `docs/research/2026-09-14-a-memory-stand-that-measures-retrieval.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_coverage as coverage  # noqa: E402

LONG_FACT = "I finally bought the Seven Husbands of Evelyn Hugo at the museum shop today"


def _question(sessions: list[str], evidence: list[str]) -> dict:
    haystack = [[{"role": "user", "content": "small talk about weather"}]]
    haystack.append([{"role": "user", "content": text, "has_answer": True} for text in evidence])
    return {"answer_session_ids": sessions, "haystack_sessions": haystack}


def _row(text: str, session: str = "") -> dict:
    return {"content": text, "heading_ancestry": [f"session_end | {session}"]}


def test_every_needed_session_found_is_all_sessions():
    question = _question(["s1", "s2"], [])
    rows = [_row("a", "s1"), _row("b", "s2")]

    result = coverage.coverage(question, rows)

    assert (result["sessions_covered"], result["all_sessions"]) == (2, True)


def test_one_of_two_sessions_is_half_recall_and_not_all():
    question = _question(["s1", "s2"], [])

    result = coverage.coverage(question, [_row("a", "s1"), _row("c", "s1")])

    assert (result["session_recall"], result["all_sessions"]) == (0.5, False)


def test_two_rows_from_one_session_count_once():
    """The old row count read this as two answer sessions retrieved."""
    question = _question(["s1", "s2"], [])

    result = coverage.coverage(question, [_row("a", "s1"), _row("b", "s1")])

    assert result["sessions_covered"] == 1


def test_an_evidence_turn_is_covered_by_its_own_text_rerendered():
    question = _question(["s1"], [LONG_FACT])
    rendered = "## [10:00] session_end | s1\n\n**user:** " + LONG_FACT.upper()

    result = coverage.coverage(question, [_row(rendered, "s1")])

    assert (result["turns_covered"], result["all_turns"]) == (1, True)


def test_a_session_found_without_its_evidence_turn_is_not_all_turns():
    """Right session, wrong span: the loss the old signal could not see."""
    question = _question(["s1"], [LONG_FACT])

    result = coverage.coverage(question, [_row("unrelated turn of the same day", "s1")])

    assert (result["all_sessions"], result["all_turns"]) == (True, False)


def test_a_question_without_labels_claims_nothing():
    result = coverage.coverage({"answer_session_ids": [], "haystack_sessions": []}, [])

    assert (result["session_recall"], result["all_sessions"], result["all_turns"]) == (
        None,
        False,
        False,
    )


def test_coverage_at_depths_reads_prefixes_of_one_ranking():
    question = _question(["s1", "s2"], [])
    rows = [_row("a", "s1")] * 12 + [_row("b", "s2")]

    result = coverage.coverage_at_depths(question, rows, depths=(12, 24))

    assert (result["k12"]["sessions_covered"], result["k24"]["sessions_covered"]) == (1, 2)


def test_a_short_evidence_turn_is_matched_whole():
    question = _question(["s1"], ["yes, 500"])

    result = coverage.coverage(question, [_row("assistant: ok. user: yes, 500", "s1")])

    assert result["turns_covered"] == 1


def test_the_middle_of_a_long_turn_counts_as_seen():
    """A chunk boundary can hand over the middle of a turn and not its first line."""
    turn = " ".join(f"sentence number {index} about the bike tune-up" for index in range(12))
    question = _question(["s1"], [turn])
    middle = coverage.normalized(turn)[200:400]

    result = coverage.coverage(question, [_row(middle, "s1")])

    assert result["turns_covered"] == 1


def test_a_different_turn_of_the_same_session_is_not_evidence():
    turn = " ".join(f"sentence number {index} about the bike tune-up" for index in range(12))
    question = _question(["s1"], [turn])

    result = coverage.coverage(question, [_row("the weather was lovely all week long in the hills", "s1")])

    assert result["turns_covered"] == 0


def _entry(all_turns: bool, turn_recall: float = 1.0) -> dict:
    return {
        "session_recall": 1.0,
        "all_sessions": True,
        "turn_recall": turn_recall,
        "all_turns": all_turns,
    }


def test_the_run_aggregate_splits_by_type_and_depth():
    rows = [
        {"question_type": "multi-session", "coverage": _entry(False, 0.5)},
        {"question_type": "multi-session", "coverage": _entry(True)},
        {"question_type": "temporal-reasoning", "coverage": _entry(True)},
    ]

    report = coverage.aggregate(rows)

    assert report["k12"]["multi-session"]["all_turns"] == 0.5
    assert report["k12"]["overall"]["n"] == 3


def test_deeper_depths_come_from_a_retrieval_only_row():
    row = {"question_type": "multi-session", "coverage_depths": {"k48": _entry(True)}}

    report = coverage.aggregate([row])

    assert list(report) == ["k48"]


def test_a_wrong_answer_is_named_by_whether_the_evidence_was_in_hand():
    rows = [
        {"judge_correct": False, "coverage": _entry(True)},
        {"judge_correct": False, "coverage": _entry(False, 0.0)},
        {"judge_correct": True, "coverage": _entry(False, 0.0)},
    ]

    assert coverage.failure_split(rows) == {
        "judged": 3,
        "wrong": 2,
        "evidence_in_hand": 1,
        "evidence_missing": 1,
    }


def test_an_unjudged_run_says_so_instead_of_reporting_no_wrong_answers():
    """`lme500.report.json` of 2026-09-17 read `wrong: 0` over 199 wrong answers.

    The report is written before the judge pass, so every verdict was absent.
    Zero wrong and zero judged has to look like silence, not like a clean run.
    """
    rows = [{"coverage": _entry(True)}, {"coverage": _entry(False, 0.0)}]

    assert coverage.failure_split(rows) == {
        "judged": 0,
        "wrong": 0,
        "evidence_in_hand": 0,
        "evidence_missing": 0,
    }
