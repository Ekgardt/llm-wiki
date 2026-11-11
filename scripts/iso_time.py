"""One reading of an ISO-8601 instant, whatever precision and zone spelling it arrives in.

A page or a model can write `2026-09-26T10:00:00.1234Z`. `normalized_iso` also gives
every stored form the same grammar and six-digit fraction, so it stays although
`datetime.fromisoformat` now reads such text by itself. Research:
docs/research/2026-09-26-one-iso-reading-for-every-python.md
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

_FRACTION = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")


def normalized_iso(text: str) -> str:
    """`Z` as `+00:00`, and a fraction of seconds padded or cut to six digits."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _FRACTION.sub(lambda match: f"{match.group(1)}.{match.group(2)[:6].ljust(6, '0')}", text)


def parse_instant(text: str) -> datetime:
    """An aware instant; a value with no zone is read as UTC. ValueError when not ISO."""
    parsed = datetime.fromisoformat(normalized_iso(text))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def utc_text(value: datetime) -> str:
    """A stored instant whose text order is its time order: UTC, six fraction digits, `Z`.

    SQL compares these as text. `isoformat()` drops the fraction on a whole
    second, and `Z` sorts after `.`, so `…:00Z` read as later than `…:00.5Z`.
    Research: docs/research/2026-09-26-every-stored-instant-has-one-width.md
    """
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def local_now() -> datetime:
    """The one clock a daily-log writer reads: the machine's wall clock, aware.

    The daily log is kept in local time, the day capture and session evidence
    already use; an aware value lets a caller convert it and never guess.
    Research: docs/research/2026-09-27-the-daily-log-keeps-one-clock.md
    """
    return datetime.now().astimezone()


def block_instant(day: str, block: str) -> str:
    """The UTC instant a daily block's local `HH:MM:SS` names, as `…Z` text."""
    local = datetime.fromisoformat(f"{day}T{block}").astimezone()
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Real zone offsets: whole quarter hours from −12:00 to +14:00 (the IANA range).
_OFFSET_STEP_SECONDS = 15 * 60
_OFFSET_RANGE_SECONDS = (-12 * 3600, 14 * 3600)


def names_block(instant: str, day: str, block: str) -> bool:
    """Whether a UTC instant is this local block's reading at some real zone offset.

    Reads no machine zone, so a ledger stays valid after the machine moves.
    """
    offset = (datetime.fromisoformat(f"{day}T{block}") - parse_instant(instant).replace(tzinfo=None)).total_seconds()
    low, high = _OFFSET_RANGE_SECONDS
    return low <= offset <= high and offset % _OFFSET_STEP_SECONDS == 0
