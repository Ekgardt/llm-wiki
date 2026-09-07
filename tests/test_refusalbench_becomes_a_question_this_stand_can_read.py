"""RefusalBench converted into the shape the LongMemEval harness already runs.

RefusalBench scores the one thing LongMemEval punishes: given a context that
is ambiguous, contradictory, missing the answer or built on a false premise,
does the system refuse — and does it still answer the ones it can. A row that
must be refused takes the `_abs` suffix, so the abstention split the scorer
already makes needs no new code.
"""

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCHMARK))

import longmemeval_data  # noqa: E402
import refusalbench_data  # noqa: E402

SINGLE = {
    "id": "RB-NQ_1",
    "expected_rag_behavior": "ANSWER_CORRECTLY",
    "perturbation_class": "P-Ambiguity",
    "intensity": "LOW",
    "perturbed_query": "who wrote it",
    "perturbed_context": "Title: A song\nIt was written by Ann.",
    "original_context": "the original, which must not be used",
    "original_answers": ["Ann", "Ann Smith"],
}
REFUSABLE = {
    "id": "RB-NQ_2",
    "expected_rag_behavior": "REFUSE_INFO_MISSING_IN_CONTEXT",
    "perturbation_class": "P-MissingInfo",
    "intensity": "HIGH",
    "perturbed_query": "who produced it",
    "perturbed_context": "Title: A song\nIt was written by Ann.",
    "original_answers": ["Bob"],
}
MULTI = {
    "id": "RB-GaRAGe_1",
    "expected_rag_behavior": "ANSWER_CORRECTLY",
    "perturbation_class": "P-Contradiction",
    "query": "what happened to the sun",
    "reference_answer": "the field formed closer to the surface",
    "grounding": [
        {"provider": "web", "cite_1": " first passage "},
        {"provider": "web", "cite_7": " seventh passage "},
        {"provider": "web"},
    ],
}


def test_a_row_that_must_be_refused_is_marked_as_one():
    [question] = refusalbench_data.converted_questions([REFUSABLE])

    assert question["question_id"].endswith("_abs")
    assert longmemeval_data.is_abstention(question)
    assert longmemeval_data.category_of(question) == "abstention"
    assert question["answer"] == ""
    assert question["refusal_expected_behavior"] == "REFUSE_INFO_MISSING_IN_CONTEXT"


def test_a_row_that_must_be_answered_keeps_the_first_gold_answer():
    [question] = refusalbench_data.converted_questions([SINGLE])

    assert not longmemeval_data.is_abstention(question)
    assert question["answer"] == "Ann"
    assert question["question_type"] == "P-Ambiguity"
    assert question["refusal_intensity"] == "LOW"


def test_the_perturbed_context_is_used_and_the_original_is_not():
    [question] = refusalbench_data.converted_questions([SINGLE])
    text = " ".join(
        turn["content"] for session in question["haystack_sessions"] for turn in session
    )

    assert "It was written by Ann." in text
    assert "must not be used" not in text


def test_every_grounding_passage_becomes_its_own_session():
    """GaRAGe numbers each passage's text `cite_<n>`, and n varies per row."""
    [question] = refusalbench_data.converted_questions([MULTI])

    assert len(question["haystack_sessions"]) == 2
    contents = [session[1]["content"] for session in question["haystack_sessions"]]
    assert contents == ["first passage", "seventh passage"]
    assert question["answer"] == "the field formed closer to the surface"


def test_a_row_with_no_context_or_no_question_is_dropped():
    empty = dict(SINGLE, perturbed_context="", original_context="")
    silent = dict(SINGLE, perturbed_query="", query="")

    assert refusalbench_data.converted_questions([empty, silent]) == []


def test_the_result_is_a_dataset_the_harness_accepts():
    questions = refusalbench_data.converted_questions([SINGLE, REFUSABLE, MULTI])

    longmemeval_data.require_dataset_shape(questions)
    assert len(questions) == 3


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path):
    with pytest.raises(refusalbench_data.DatasetUnavailable, match="never generates"):
        refusalbench_data.load_rows(tmp_path / "absent.jsonl")
