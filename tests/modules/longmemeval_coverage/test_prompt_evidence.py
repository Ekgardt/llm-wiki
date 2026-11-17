"""Original prompt and compile coverage assertions, separate from worker execution."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_coverage.fixtures import _EVIDENCE, _OTHER, _QUESTION

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'benchmark'))

import longmemeval_coverage  # noqa: E402


def test_the_evidence_turns_of_a_prompt_are_read_from_the_dataset_labels() -> None:
    """The flagged turn is found in the prompt; the unflagged one is not asked about."""
    seen = longmemeval_coverage.evidence_in_text(_QUESTION, f"context: {_EVIDENCE.upper()}")

    assert (seen["turns_labelled"], seen["turns_in_text"]) == (1, 1)
    assert seen["all_turns"] is True

def test_a_prompt_without_the_evidence_turn_says_so() -> None:
    absent = longmemeval_coverage.evidence_in_text(_QUESTION, f"context: {_OTHER}")

    assert (absent["turns_labelled"], absent["turns_in_text"]) == (1, 0)
    assert absent["all_turns"] is False

def test_the_compiler_stage_has_its_own_name_and_its_own_counts() -> None:
    """`evidence_requested`/`evidence_missed` is a different stage from retrieval."""
    rows = [{"evidence_requested": 8, "evidence_missed": 2}, {"evidence_requested": 2}]

    assert longmemeval_coverage.compile_evidence(rows) == {
        "chunks_requested": 10,
        "chunks_missed": 2,
        "chunks_placed_share": 0.8,
    }
