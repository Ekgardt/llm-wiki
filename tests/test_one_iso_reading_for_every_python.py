"""An instant a page writes has one reading (audit 2026-09-26 C-4).

docs/research/2026-09-26-one-iso-reading-for-every-python.md
"""
from __future__ import annotations

from datetime import UTC, datetime

import contradiction_pipeline


def test_the_contradiction_pipeline_reads_a_short_fraction() -> None:
    assert contradiction_pipeline.instant("2026-09-26T10:00:00.5Z") == datetime(2026, 9, 26, 10, 0, 0, 500000, tzinfo=UTC)
