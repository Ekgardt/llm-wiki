"""One reading of an ISO-8601 instant that Python 3.10 and later agree on.

`datetime.fromisoformat` learned the `Z` suffix and fractions of any length only
in 3.11; 3.10, the lowest supported version, refuses both. A page or a model can
write `2026-09-26T10:00:00.1234Z`. Research:
docs/research/2026-09-26-one-iso-reading-for-every-python.md
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

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
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
