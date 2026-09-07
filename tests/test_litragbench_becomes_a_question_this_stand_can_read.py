"""LIT-RAGBench converted into the shape the LongMemEval harness already runs.

114 questions over five capabilities, sixty of them abstention: the context is
cut off, self-contradictory, or nothing but decoys. Those sixty are more than
half of what LongMemEval punishes this product for, which is why a stand this
small is worth running.
"""

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCHMARK))

import litragbench_data  # noqa: E402
import longmemeval_data  # noqa: E402

ANSWERABLE = {
    "qa_type": ["R_multihop"],
    "question": "Where did it place?",
    "answer": "29th place",
    "positive_chunk_list": [{"title": "Ranking", "content": "It rose one place."}],
    "negative_chunk_list": [{"title": "", "content": "An unrelated passage."}],
}
ABSTAINING = {
    "qa_type": ["A_negative_only"],
    "question": "Where does the oil come from?",
    "answer": "Based on the information provided, it is not possible to determine.",
    "positive_chunk_list": [],
    "negative_chunk_list": [{"title": "", "content": "A decoy passage."}],
}


def test_an_abstention_row_is_right_when_nothing_is_said():
    [question] = litragbench_data.converted_questions([ABSTAINING])

    assert question["question_id"].endswith("_abs")
    assert longmemeval_data.is_abstention(question)
    assert question["answer"] == ""
    assert question["litrag_reference_answer"].startswith("Based on the information")
    assert question["question_type"] == "abstention"


def test_an_answerable_row_keeps_its_answer_and_names_its_capability():
    [question] = litragbench_data.converted_questions([ANSWERABLE])

    assert not longmemeval_data.is_abstention(question)
    assert question["answer"] == "29th place"
    assert question["question_type"] == "reasoning"
    assert question["litrag_code"] == "R_multihop"


def test_the_decoys_are_ingested_too():
    """Only the answer in the corpus is not the task."""
    [question] = litragbench_data.converted_questions([ANSWERABLE])
    contents = [session[1]["content"] for session in question["haystack_sessions"]]

    assert contents == ["Ranking\nIt rose one place.", "An unrelated passage."]


def test_every_capability_letter_has_a_name():
    for code, name in (
        ("I_multiple", "integration"),
        ("L_synonym", "logic"),
        ("R_calculate", "reasoning"),
        ("T_html", "table"),
        ("A_conflicted", "abstention"),
    ):
        [question] = litragbench_data.converted_questions(
            [dict(ANSWERABLE, qa_type=[code])]
        )
        assert question["question_type"] == name


def test_a_row_with_no_chunks_or_no_question_is_dropped():
    empty = dict(ANSWERABLE, positive_chunk_list=[], negative_chunk_list=[])
    silent = dict(ANSWERABLE, question="  ")

    assert litragbench_data.converted_questions([empty, silent]) == []


def test_the_result_is_a_dataset_the_harness_accepts():
    questions = litragbench_data.converted_questions([ANSWERABLE, ABSTAINING])

    longmemeval_data.require_dataset_shape(questions)
    assert len(questions) == 2


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path):
    with pytest.raises(litragbench_data.DatasetUnavailable, match="never generates"):
        litragbench_data.load_rows(tmp_path / "absent.jsonl")
