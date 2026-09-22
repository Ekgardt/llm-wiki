"""A LoCoMo question's `D7:19` references become the labels the stand reads.

The stand counts coverage from `answer_session_ids` and `has_answer`, and the
converted LoCoMo question carried neither: on the recorded run of 2026-09-19
`coverage.sessions_labelled` was 0 on all 300 rows and no twin could be built.
The references are written as labels where the question set is prepared, and
a LongMemEval question, which labels its own evidence, is left untouched.
See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_coverage  # noqa: E402
import longmemeval_data  # noqa: E402
import run_longmemeval  # noqa: E402


def _locomo_question(**fields: object) -> dict:
    return {
        "question_id": "conv-26_7",
        "question_type": "single-hop",
        "question": "What did Melanie buy?",
        "answer": "new shoes",
        "question_date": "2023/06/01 (Thu) 12:00",
        "haystack_session_ids": ["session_1", "session_2"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/22 (Mon) 09:00"],
        "haystack_sessions": [
            [{"role": "user", "content": "Hi there."}, {"role": "assistant", "content": "Hello."}],
            [{"role": "user", "content": "I ran today."}, {"role": "assistant", "content": "I bought new shoes."}],
        ],
        "answer_session_ids": [],
        "locomo_evidence": ["D2:2"],
        **fields,
    }


def test_the_references_become_session_ids_and_has_answer_on_the_named_turn() -> None:
    labelled = longmemeval_data.labelled_by_evidence(_locomo_question())

    assert labelled["answer_session_ids"] == ["session_2"]
    assert [turn.get("has_answer") for turn in labelled["haystack_sessions"][1]] == [None, True]
    assert [turn.get("has_answer") for turn in labelled["haystack_sessions"][0]] == [None, None]


def test_the_stand_then_counts_the_evidence_it_could_not_see_before() -> None:
    labelled = longmemeval_data.labelled_by_evidence(_locomo_question())
    rows = [{"content": "I bought new shoes.", "heading_ancestry": ["session_2"], "path": "knowledge/daily/2023-05-22.md"}]

    covered = longmemeval_coverage.coverage(labelled, rows)

    assert (covered["sessions_labelled"], covered["turns_labelled"]) == (1, 1)
    assert (covered["all_sessions"], covered["all_turns"]) == (True, True)
    assert longmemeval_data.twin_of(labelled)["haystack_session_ids"] == ["session_1"]


def test_a_reference_to_a_session_the_haystack_lacks_labels_nothing_and_a_labelled_question_is_untouched() -> None:
    absent = longmemeval_data.labelled_by_evidence(_locomo_question(locomo_evidence=["D9:1"]))
    own = _locomo_question(answer_session_ids=["session_1"], locomo_evidence=["D2:2"])

    assert absent["answer_session_ids"] == []
    assert longmemeval_data.labelled_by_evidence(own) is own


def test_the_runner_labels_a_dataset_file_as_it_loads_it(tmp_path: Path) -> None:
    dataset = tmp_path / "locomo_s.json"
    dataset.write_text(json.dumps([_locomo_question()]), encoding="utf-8")

    loaded = run_longmemeval._dataset(argparse.Namespace(dataset=str(dataset)))

    assert loaded[0]["answer_session_ids"] == ["session_2"]
