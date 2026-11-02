"""Timestamp reading remains identical across supported Python versions."""
from datetime import datetime, timezone

import pytest
from iso_time import parse_instant

EXPECTED = datetime(2026, 9, 26, 10, 0, 0, 123400, tzinfo=timezone.utc)

@pytest.mark.parametrize(
    "text",
    ["2026-09-26T10:00:00.1234Z", "2026-09-26T10:00:00.1234+00:00", "2026-09-26T10:00:00.123400Z", "2026-09-26T10:00:00.12340000Z"],
)
def test_any_fraction_and_z_are_read(text: str) -> None:
    assert parse_instant(text) == EXPECTED

