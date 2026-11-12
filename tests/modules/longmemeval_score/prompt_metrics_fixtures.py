"""Single-owner synthetic metric fixtures shared by integrations."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def _row(category: str, **fields: object) -> dict:
    return {
        "category": category,
        "question_type": category,
        "gold": "the paid tier",
        "hypothesis": "You moved to the paid tier.",
        "status": "answered",
        **fields,
    }


# A gold that is a span somebody said, and it reached the prompt.
_SPAN_GOLD = _row(
    "multi-session",
    gold_in_prompt=True,
    evidence_turns_labelled=1,
    evidence_in_prompt=True,
)


# A gold that is a rubric: no prompt can contain it, so the gold signal is held
# out — but the dataset's evidence turn still reached the reader and is counted.
_RUBRIC_GOLD = _row(
    "single-session-preference",
    gold_in_prompt=False,
    evidence_turns_labelled=1,
    evidence_in_prompt=True,
)


# A span gold whose evidence never arrived: a real miss, and it must stay one.
_MISSING_EVIDENCE = _row(
    "temporal-reasoning",
    gold_in_prompt=False,
    evidence_turns_labelled=1,
    evidence_in_prompt=False,
)


_ALL = [_SPAN_GOLD, _RUBRIC_GOLD, _MISSING_EVIDENCE]


def _overall() -> dict:
    return longmemeval_score.aggregate(_ALL)["overall"]
