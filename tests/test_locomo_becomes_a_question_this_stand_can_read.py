"""LoCoMo converted into the shape the LongMemEval harness already runs.

The point of the conversion is the 446 category-5 questions: they name
something the conversation never says, and the dataset ships the plausible
wrong answer a system hallucinates instead of the right one. Silence is the
correct answer, which is what this product is built to give and what
LongMemEval scores against it. They take the `_abs` suffix so
`longmemeval_data.is_abstention` recognises them with no new code.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCHMARK))

import locomo_data  # noqa: E402
import longmemeval_data  # noqa: E402

CONVERSATION = {
    "sample_id": "conv-1",
    "conversation": {
        "session_1": [
            {"speaker": "Ann", "dia_id": "D1:1", "text": "I ran a race."},
            {"speaker": "Bob", "dia_id": "D1:2", "text": "How was it?"},
        ],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_2": [
            {
                "speaker": "Ann",
                "dia_id": "D2:1",
                "text": "Look at this.",
                "img_url": "http://example.invalid/a.png",
                "blip_caption": "a medal on a table",
            },
        ],
        "session_2_date_time": "9:05 am on 12 June, 2023",
    },
    "qa": [
        {"question": "What did Ann do?", "answer": "ran a race", "evidence": ["D1:1"], "category": 4},
        {
            "question": "What did Ann realise afterwards?",
            "evidence": ["D1:1"],
            "category": 5,
            "adversarial_answer": "that rest matters",
        },
    ],
}


def test_a_timestamp_is_read_and_written_in_the_shape_the_harness_carries():
    assert locomo_data.parse_when("1:56 pm on 8 May, 2023") == datetime(2023, 5, 8, 13, 56)
    assert locomo_data.parse_when("12:30 am on 1 January, 2024") == datetime(2024, 1, 1, 0, 30)
    assert locomo_data.parse_when("12:30 pm on 1 January, 2024") == datetime(2024, 1, 1, 12, 30)
    assert locomo_data.format_when(datetime(2023, 5, 8, 13, 56)) == "2023/05/08 (Mon) 13:56"

    with pytest.raises(ValueError, match="unrecognised"):
        locomo_data.parse_when("yesterday")


def test_the_first_speaker_is_the_user_and_the_second_the_assistant():
    [answerable, _] = locomo_data.converted_questions([CONVERSATION])
    roles = [turn["role"] for session in answerable["haystack_sessions"] for turn in session]

    assert roles == ["user", "assistant", "user"]


def test_an_image_arrives_as_its_caption_and_says_so():
    [answerable, _] = locomo_data.converted_questions([CONVERSATION])
    content = answerable["haystack_sessions"][1][0]["content"]

    assert content == "Look at this.\n[image: a medal on a table]"


def test_an_adversarial_question_is_marked_as_one_the_answer_to_which_is_silence():
    [_, adversarial] = locomo_data.converted_questions([CONVERSATION])

    assert adversarial["question_id"].endswith("_abs")
    assert longmemeval_data.is_abstention(adversarial)
    assert longmemeval_data.category_of(adversarial) == "abstention"
    assert adversarial["answer"] == ""
    assert adversarial["locomo_adversarial_answer"] == "that rest matters"


def test_an_answerable_question_keeps_its_answer_and_its_category():
    [answerable, _] = locomo_data.converted_questions([CONVERSATION])

    assert not longmemeval_data.is_abstention(answerable)
    assert answerable["question_type"] == "single-hop"
    assert answerable["answer"] == "ran a race"


def test_the_question_is_asked_after_the_last_session():
    [answerable, _] = locomo_data.converted_questions([CONVERSATION])

    assert answerable["haystack_dates"] == [
        "2023/05/08 (Mon) 13:56",
        "2023/06/12 (Mon) 09:05",
    ]
    assert answerable["question_date"] > answerable["haystack_dates"][-1]


def test_the_result_is_a_dataset_the_harness_accepts():
    questions = locomo_data.converted_questions([CONVERSATION])

    longmemeval_data.require_dataset_shape(questions)


def test_sessions_come_out_in_number_order_not_string_order():
    conversation = {
        "sample_id": "conv-2",
        "conversation": {
            f"session_{n}": [{"speaker": "Ann", "text": f"turn {n}"}] for n in (1, 2, 10)
        }
        | {
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_2_date_time": "1:00 pm on 2 May, 2023",
            "session_10_date_time": "1:00 pm on 10 May, 2023",
        },
        "qa": [{"question": "q", "answer": "a", "category": 4}],
    }

    [question] = locomo_data.converted_questions([conversation])

    assert question["haystack_session_ids"] == ["session_1", "session_2", "session_10"]


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path):
    with pytest.raises(locomo_data.DatasetUnavailable, match="never generates"):
        locomo_data.load_source(tmp_path / "absent.json")
