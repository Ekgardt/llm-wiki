"""Original pure regression, unchanged AST and assertions."""
from datetime import UTC, datetime

from llm_wiki import ownership_timing as ownership


def test_both_timestamp_shapes_are_read_and_one_is_written() -> None:
    whole = datetime(2026, 9, 17, 12, tzinfo=UTC)
    written = ownership._timestamp(whole)
    read = (
        ownership._parse_timestamp("2026-09-17T12:00:00Z"),
        ownership._parse_timestamp(written),
    )
    assert (written, read) == ("2026-09-17T12:00:00.000000Z", (whole, whole))
