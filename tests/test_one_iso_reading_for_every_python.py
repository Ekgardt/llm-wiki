"""An instant a page writes is read the same on Python 3.10 and 3.11+ (audit 2026-09-26 C-4).

docs/research/2026-09-26-one-iso-reading-for-every-python.md
"""
from __future__ import annotations

from datetime import datetime, timezone

import contradiction_pipeline
import pytest
from iso_time import parse_instant

EXPECTED = datetime(2026, 9, 26, 10, 0, 0, 123400, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text",
    ["2026-09-26T10:00:00.1234Z", "2026-09-26T10:00:00.1234+00:00", "2026-09-26T10:00:00.123400Z", "2026-09-26T10:00:00.12340000Z"],
)
def test_any_fraction_and_z_are_read(text: str) -> None:
    assert parse_instant(text) == EXPECTED


def test_the_contradiction_pipeline_reads_a_short_fraction() -> None:
    assert contradiction_pipeline._instant("2026-09-26T10:00:00.5Z") == datetime(2026, 9, 26, 10, 0, 0, 500000, tzinfo=timezone.utc)
