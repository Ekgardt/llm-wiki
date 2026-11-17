"""Original dataset preparation contracts, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_data.twin_fixture import _question

BENCHMARK = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_data  # noqa: E402


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
