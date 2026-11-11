"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_score.splits_fixtures import _row

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def test_the_report_carries_each_half_of_the_stand_on_its_own() -> None:
    rows = [_row(question_id=f"id{n}", status="answered", hypothesis="a gravel bike") for n in range(20)]

    splits = longmemeval_score.split_calibration(rows)

    assert set(splits) == {"tune", "decide"}
    assert splits["tune"]["answerable"] + splits["decide"]["answerable"] == 20
