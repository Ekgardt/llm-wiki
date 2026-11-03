"""Refitting the lane constants is one command over a run's own records.

Third audit, 2026-09-17 (retrieval M5, L1). The constants in `scripts/lane_score.py` were
fitted by probes that live in no repository, so nothing here could refit them; and the stand's
records did not carry the per-candidate lane matrix a refit needs. The refit itself waits for
a measurement run the owner must permit — this proves the command works on a synthetic file.
See `docs/research/2026-09-17-the-lane-score-refit-is-one-command.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "benchmark"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


pytest.importorskip("numpy")


def test_the_stand_records_the_rows_a_refit_needs() -> None:
    import longmemeval_vault

    question = {
        "haystack_sessions": [[{"content": "I sold the drum set last May.", "has_answer": True}]]
    }
    daily = "knowledge/daily/2023-05-26.md"
    rows = [
        {
            "content": "**user:** I sold the drum set last May.",
            "bm25_rank": 1,
            "vector_rank": 2,
            "path": daily,
            "heading_ancestry": ["Session one"],
        },
        {"content": "**assistant:** Noted.", "bm25_rank": 5, "vector_rank": None, "path": daily},
    ]

    matrix = longmemeval_vault.lane_matrix(question, rows)

    assert [row["evidence"] for row in matrix] == [True, False]
    assert [row["user_turn"] for row in matrix] == [True, False]
    assert [row["lexical_rank"] for row in matrix] == [1, 5]
    assert [row["source"] for row in matrix] == [[daily, ["Session one"]], [daily, []]]
