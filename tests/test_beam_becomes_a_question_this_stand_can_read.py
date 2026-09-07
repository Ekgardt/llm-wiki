"""BEAM converted into the shape the LongMemEval harness already runs.

Only the abilities whose probes carry a gold answer are converted: this stand
scores a string and a judge, not a rubric, and pretending a rubric is a string
would report a number that means nothing.
"""

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCHMARK))

import beam_data  # noqa: E402
import longmemeval_data  # noqa: E402

CONVERSATION = {
    "conversation_id": "7",
    "chat": [
        [
            {"role": "user", "content": "First thing I said. ->-> 1,1", "time_anchor": "March-15-2024"},
            {"role": "assistant", "content": "First reply. ->-> 1,2", "time_anchor": "None"},
        ],
        [
            {"role": "user", "content": "Later thing. ->-> 2,1", "time_anchor": "April-05-2024"},
        ],
    ],
    "probing_questions": str(
        {
            "abstention": [
                {
                    "question": "What did the review say?",
                    "ideal_response": "Based on the provided chat, there is no information.",
                    "why_unanswerable": "never discussed",
                    "difficulty": "medium",
                }
            ],
            "temporal_reasoning": [
                {"question": "When did it start?", "answer": "15 March 2024"}
            ],
            "summarization": [
                {"question": "Summarise it", "ideal_summary": "a summary"}
            ],
            "instruction_following": [
                {"question": "Did I comply?", "expected_compliance": "yes"}
            ],
        }
    ),
}


def _by_type(questions, name):
    return [q for q in questions if q["question_type"] == name]


def test_an_abstention_probe_is_right_when_nothing_is_said():
    [question] = _by_type(beam_data.converted_questions([CONVERSATION]), "abstention")

    assert question["question_id"].endswith("_abs")
    assert longmemeval_data.is_abstention(question)
    assert question["answer"] == ""
    assert question["beam_why_unanswerable"] == "never discussed"
    assert question["beam_reference_answer"].startswith("Based on the provided chat")


def test_an_answerable_probe_keeps_its_gold_answer():
    [question] = _by_type(
        beam_data.converted_questions([CONVERSATION]), "temporal_reasoning"
    )

    assert not longmemeval_data.is_abstention(question)
    assert question["answer"] == "15 March 2024"


def test_a_rubric_scored_ability_is_not_converted():
    """Pretending a rubric is a string would report a number meaning nothing."""
    questions = beam_data.converted_questions([CONVERSATION])

    assert _by_type(questions, "summarization") == []
    assert _by_type(questions, "instruction_following") == []
    assert len(questions) == 2


def test_a_batch_becomes_a_session_dated_by_its_anchor():
    [question, _] = beam_data.converted_questions([CONVERSATION])

    assert question["haystack_dates"] == [
        "2024/03/15 (Fri) 09:00",
        "2024/04/05 (Fri) 09:00",
    ]
    assert len(question["haystack_sessions"]) == 2
    assert question["question_date"] > question["haystack_dates"][-1]


def test_the_batch_and_turn_marker_is_not_something_anyone_said():
    [question, _] = beam_data.converted_questions([CONVERSATION])
    contents = [turn["content"] for turn in question["haystack_sessions"][0]]

    assert contents == ["First thing I said.", "First reply."]


def test_the_result_is_a_dataset_the_harness_accepts():
    longmemeval_data.require_dataset_shape(
        beam_data.converted_questions([CONVERSATION])
    )


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path):
    with pytest.raises(beam_data.DatasetUnavailable, match="never generates"):
        beam_data.load_rows(tmp_path / "absent.parquet")
