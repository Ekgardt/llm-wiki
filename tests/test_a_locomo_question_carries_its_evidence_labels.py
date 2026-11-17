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

from tests.modules.longmemeval_data.label_fixture import _locomo_question

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_coverage  # noqa: E402
import longmemeval_data  # noqa: E402
import run_longmemeval  # noqa: E402


def test_the_stand_then_counts_the_evidence_it_could_not_see_before() -> None:
    labelled = longmemeval_data.labelled_by_evidence(_locomo_question())
    rows = [{"content": "I bought new shoes.", "heading_ancestry": ["session_2"], "path": "knowledge/daily/2023-05-22.md"}]

    covered = longmemeval_coverage.coverage(labelled, rows)

    assert (covered["sessions_labelled"], covered["turns_labelled"]) == (1, 1)
    assert (covered["all_sessions"], covered["all_turns"]) == (True, True)
    assert longmemeval_data.twin_of(labelled)["haystack_session_ids"] == ["session_1"]


def test_the_runner_labels_a_dataset_file_as_it_loads_it(tmp_path: Path) -> None:
    dataset = tmp_path / "locomo_s.json"
    dataset.write_text(json.dumps([_locomo_question()]), encoding="utf-8")

    loaded = run_longmemeval._dataset(argparse.Namespace(dataset=str(dataset)))

    assert loaded[0]["answer_session_ids"] == ["session_2"]
