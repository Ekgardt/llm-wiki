"""Original dataset preparation contracts, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_data  # noqa: E402


def _question(question_id: str, question_type: str) -> dict:
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": "q",
        "answer": "a",
        "question_date": "2023/05/30 (Tue) 23:40",
        "haystack_dates": [],
        "haystack_session_ids": [],
        "haystack_sessions": [],
    }


def _population() -> list[dict]:
    rows = [_question(f"m{index}", "multi-session") for index in range(300)]
    rows += [_question(f"t{index}", "temporal-reasoning") for index in range(150)]
    rows += [_question(f"k{index}_abs", "knowledge-update") for index in range(50)]
    return rows


def _sampled_ids(seed: int) -> list[str]:
    picked = longmemeval_data.stratified_sample(_population(), 50, seed=seed)
    return [question["question_id"] for question in picked]


def test_the_sample_is_deterministic_for_a_seed() -> None:
    assert _sampled_ids(13) == _sampled_ids(13)


def test_the_sample_has_the_asked_size_without_duplicates() -> None:
    ids = _sampled_ids(13)

    assert len(ids) == 50
    assert len(set(ids)) == 50


def test_the_sample_is_proportional_across_strata() -> None:
    picked = longmemeval_data.stratified_sample(_population(), 50, seed=13)
    types = [question["question_type"] for question in picked]

    assert types.count("multi-session") == 30
    assert types.count("temporal-reasoning") == 15
    assert types.count("knowledge-update") == 5


def test_abstention_questions_are_their_own_category() -> None:
    plain = _question("q1", "knowledge-update")
    abstention = _question("q2_abs", "knowledge-update")

    assert longmemeval_data.category_of(plain) == "knowledge-update"
    assert longmemeval_data.category_of(abstention) == "abstention"


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path: Path) -> None:
    with pytest.raises(longmemeval_data.DatasetUnavailable) as caught:
        longmemeval_data.load_dataset(tmp_path / "absent.json")

    assert "synthetic" in str(caught.value)
