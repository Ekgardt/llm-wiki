"""Original dataset preparation contracts, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_data.label_fixture import _locomo_question

BENCHMARK = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_data  # noqa: E402


def test_the_references_become_session_ids_and_has_answer_on_the_named_turn() -> None:
    labelled = longmemeval_data.labelled_by_evidence(_locomo_question())

    assert labelled["answer_session_ids"] == ["session_2"]
    assert [turn.get("has_answer") for turn in labelled["haystack_sessions"][1]] == [None, True]
    assert [turn.get("has_answer") for turn in labelled["haystack_sessions"][0]] == [None, None]


def test_a_reference_to_a_session_the_haystack_lacks_labels_nothing_and_a_labelled_question_is_untouched() -> None:
    absent = longmemeval_data.labelled_by_evidence(_locomo_question(locomo_evidence=["D9:1"]))
    own = _locomo_question(answer_session_ids=["session_1"], locomo_evidence=["D2:2"])

    assert absent["answer_session_ids"] == []
    assert longmemeval_data.labelled_by_evidence(own) is own
