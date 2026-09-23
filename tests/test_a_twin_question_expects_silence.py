"""A twin is the same question with its evidence removed, and silence is its answer.

Both refusal errors are measured on one question without a new label: the
original, answered right, and its twin, refused. The twin is built where the
question set is prepared, the scorer treats it as a silence-expected row, the
judge never sees it, and the tune / decide split is fixed by the question id
before any threshold is set.
See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_data  # noqa: E402
import longmemeval_judge  # noqa: E402
import longmemeval_score  # noqa: E402
import run_longmemeval  # noqa: E402


def _question(**fields: object) -> dict:
    return {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "Which bike did I buy?",
        "answer": "a gravel bike",
        "question_date": "2023/05/30 (Tue) 20:42",
        "haystack_session_ids": ["s1", "s2", "s3"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/22 (Mon) 09:00", "2023/05/25 (Thu) 18:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "I bought a gravel bike.", "has_answer": True}],
            [{"role": "user", "content": "The weather was fine."}],
            [{"role": "user", "content": "My commuter bike needs a chain."}],
        ],
        "answer_session_ids": ["s1"],
        **fields,
    }


def test_a_twin_loses_exactly_its_evidence_sessions_and_keeps_the_rest_aligned() -> None:
    twin = longmemeval_data.twin_of(_question())

    assert (twin["question_id"], twin["twin_of"], twin["answer"]) == ("q1_twin", "q1", "")
    assert twin["haystack_session_ids"] == ["s2", "s3"]
    assert twin["haystack_dates"] == ["2023/05/22 (Mon) 09:00", "2023/05/25 (Thu) 18:00"]
    assert [turn["content"] for session in twin["haystack_sessions"] for turn in session] == [
        "The weather was fine.",
        "My commuter bike needs a chain.",
    ]


def test_a_locomo_question_names_its_evidence_sessions_by_turn_reference() -> None:
    question = _question(
        haystack_session_ids=["session_1", "session_7"],
        haystack_dates=["2023/05/20 (Sat) 02:21", "2023/05/22 (Mon) 09:00"],
        haystack_sessions=[[{"role": "user", "content": "a"}], [{"role": "user", "content": "b"}]],
        answer_session_ids=[],
        locomo_evidence=["D7:19"],
    )

    twin = longmemeval_data.twin_of(question)

    assert longmemeval_data.evidence_sessions_of(question) == {"session_7"}
    assert (twin["haystack_session_ids"], twin["locomo_evidence"]) == (["session_1"], [])


def test_a_question_with_nothing_to_remove_has_no_twin() -> None:
    silent = _question(question_id="q1_abs")
    unlabelled = _question(answer_session_ids=[])

    assert (longmemeval_data.twin_of(silent), longmemeval_data.twin_of(unlabelled)) == (None, None)


def test_a_twin_is_its_own_category_and_expects_silence() -> None:
    twin = longmemeval_data.twin_of(_question())

    assert longmemeval_data.category_of(twin) == "twin"
    assert (longmemeval_data.expects_silence(twin), longmemeval_data.is_abstention(twin)) == (True, False)
    assert longmemeval_data.original_of("q1_twin") == "q1"


def test_the_split_is_fixed_by_the_id_and_a_twin_follows_its_original() -> None:
    halves = {longmemeval_data.split_of(f"id{n}") for n in range(40)}

    assert halves == {"tune", "decide"}
    assert longmemeval_data.split_of("q1_twin") == longmemeval_data.split_of("q1")
    assert (longmemeval_data.split_of("conv-26_3"), longmemeval_data.split_of("conv-50_191_abs")) == ("tune", "decide")


def _row(**fields: object) -> dict:
    return {"question_id": "q1", "category": "single-session-user", "gold": "a gravel bike", **fields}


def test_a_refused_twin_scores_right_and_an_answered_twin_scores_wrong() -> None:
    refused = _row(question_id="q1_twin", category="twin", status="insufficient_evidence", hypothesis="")
    answered = _row(question_id="q1_twin", category="twin", status="answered", hypothesis="A road bike.")

    assert longmemeval_score.score_question(refused)["correct"] is True
    assert longmemeval_score.score_question(answered)["correct"] is False
    assert (longmemeval_judge.needs_judging(answered), longmemeval_judge.needs_judging(refused)) == (False, False)


def test_a_pair_is_right_only_when_the_original_answered_right_and_the_twin_refused() -> None:
    rows = [
        _row(status="answered", hypothesis="a gravel bike", judge_correct=True),
        _row(question_id="q1_twin", category="twin", status="insufficient_evidence", hypothesis=""),
        _row(question_id="q2", status="answered", hypothesis="a road bike", judge_correct=False),
        _row(question_id="q2_twin", category="twin", status="answered", hypothesis="a road bike"),
    ]

    calibration = longmemeval_score.abstention_calibration(rows)

    assert (calibration["twins"], calibration["refused_when_twin"], calibration["answered_when_twin"]) == (2, 1, 1)
    assert (calibration["pairs"], calibration["pairs_right"]) == (2, 1)
    assert "refused_when_twin=1/2" in run_longmemeval.abstention_line(calibration)
    assert "pairs_right=1/2" in run_longmemeval.abstention_line(calibration)


def test_the_runner_appends_one_twin_per_answerable_question_and_names_the_arm() -> None:
    sample = [_question(), _question(question_id="q2_abs")]
    args = argparse.Namespace(full=False, sample=2, seed=1, twins=True)

    questions = run_longmemeval.with_twins(sample)

    assert [q["question_id"] for q in questions] == ["q1", "q2_abs", "q1_twin"]
    assert run_longmemeval._run_tag(args) == "n2-seed1-twins"


def test_the_report_carries_each_half_of_the_stand_on_its_own() -> None:
    rows = [_row(question_id=f"id{n}", status="answered", hypothesis="a gravel bike") for n in range(20)]

    splits = longmemeval_score.split_calibration(rows)

    assert set(splits) == {"tune", "decide"}
    assert splits["tune"]["answerable"] + splits["decide"]["answerable"] == 20
