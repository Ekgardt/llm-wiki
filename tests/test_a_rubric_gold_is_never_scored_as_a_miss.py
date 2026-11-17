"""A gold that describes a good answer is not a gold any text metric can read.

LongMemEval grades one question type against a rubric. The authors' own template
hands the judge "a rubric for desired personalized response", and the gold text
was written by them, not said by anyone in the sessions: "The user would prefer
responses that suggest resources specifically tailored to Adobe Premiere Pro".

No answer contains that and no prompt can either, so `contains`, `em` and `f1`
read zero there whatever the system does. On the recorded run of 2026-09-17 the
type read 0.0 by containment and the judge called right all three rows it
managed to grade — and the published judge report said 0.1, because the judge's
silence on the other twenty-seven was filled in with the same containment test.

An undefined metric reports nothing. It never reports a miss.
See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_score.rubrics_fixtures import _RUBRIC, _SPAN

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_judge  # noqa: E402
import longmemeval_score  # noqa: E402


def test_an_ungraded_rubric_row_is_silence_and_not_a_wrong_answer() -> None:
    """The judge said nothing; the substring test must not answer for it."""
    report = longmemeval_judge._judge_accuracy([{**_RUBRIC, "judge_correct": None}])
    row = report["single-session-preference"]

    assert (row["n"], row["ungraded"]) == (0, 1)
    assert row["judge_accuracy"] is None


def test_an_ungraded_fact_row_still_falls_back_to_the_text_score() -> None:
    """Where containment means something it stays; only the undefined case changed."""
    report = longmemeval_judge._judge_accuracy([{**_SPAN, "judge_correct": None}])
    row = report["multi-session"]

    assert (row["n"], row["ungraded"]) == (1, 0)
    assert row["judge_accuracy"] == 1.0


def test_the_report_says_how_much_of_the_mean_the_judge_actually_spoke() -> None:
    """The figure is a blend of two metrics, and it never said which was which."""
    rows = [{**_SPAN, "judge_correct": True}, {**_SPAN, "judge_correct": None}]
    row = longmemeval_judge._judge_accuracy(rows)["multi-session"]

    assert (row["judged"], row["from_text_score"]) == (1, 1)
    assert (row["n"], row["judge_accuracy"]) == (2, 1.0)


def test_a_graded_rubric_row_keeps_the_judges_word() -> None:
    report = longmemeval_judge._judge_accuracy([{**_RUBRIC, "judge_correct": True}])
    row = report["single-session-preference"]

    assert (row["n"], row["judged"], row["ungraded"]) == (1, 1, 0)
    assert row["judge_accuracy"] == 1.0


def test_the_judge_and_the_scorer_read_one_list_of_rubric_categories() -> None:
    """Two copies would drift, and the metrics would hold out different rows."""
    assert longmemeval_judge.gold_is_rubric is longmemeval_score.gold_is_rubric
    assert longmemeval_judge.system_prompt_for(_RUBRIC) is longmemeval_judge.RUBRIC_SYSTEM_PROMPT
