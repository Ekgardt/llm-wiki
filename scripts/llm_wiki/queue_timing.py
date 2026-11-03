"""Queue timestamp values and retry calculations, without storage or execution."""
from __future__ import annotations

import email.utils
import math
import random
from datetime import UTC, datetime, timedelta

from llm_wiki.reliability_defaults import DEFAULTS

# Existing policy: Retry-After is capped at a week; bool is checked before int.
# See docs/research/2026-09-17-the-adopted-queue-waits-as-long-as-it-was-told.md.
_MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60

def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")

def _parse_timestamp(value: str | None) -> datetime | None:
    return _as_utc(datetime.fromisoformat(value)) if value is not None else None

def _no_retry_after(_value: object, _now: datetime) -> float | None:
    """A bool is not a delay, however int-like Python thinks it is."""
    return None

def _retry_after_number(value: object, _now: datetime) -> float | None:
    # Bound Python's arbitrary-size integers before converting to a machine float.
    if isinstance(value, int):
        value = max(-1, min(value, _MAX_RETRY_AFTER_SECONDS))
    return _bounded_retry_number(float(value))  # type: ignore[arg-type]

def _retry_after_deadline(value: object, now: datetime) -> float | None:
    return _bounded_retry_until(value, now)  # type: ignore[arg-type]

def _retry_after_text(value: object, now: datetime) -> float | None:
    return _retry_after_from_text(str(value), now)

_RETRY_AFTER_READERS = (
    (bool, _no_retry_after),
    ((int, float), _retry_after_number),
    (datetime, _retry_after_deadline),
    (str, _retry_after_text),
)

def _bounded_retry_number(value: float) -> float | None:
    """A retry delay in seconds, within its bound; a bad number is no delay."""
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, float(_MAX_RETRY_AFTER_SECONDS))

def _bounded_retry_until(value: datetime, now: datetime) -> float:
    """The wait until a moment, never negative and never past the bound."""
    seconds = max(0.0, (_as_utc(value) - now).total_seconds())
    return min(seconds, float(_MAX_RETRY_AFTER_SECONDS))

def _retry_after_from_text(value: str, now: datetime) -> float | None:
    """A Retry-After header: either a count of seconds or an HTTP date."""
    stripped = value.strip()
    try:
        if stripped.isdigit():
            return _bounded_retry_number(float(stripped))
        parsed = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    return _bounded_retry_until(parsed, now)

def _retry_after_seconds(
    value: float | datetime | str | None, now: datetime
) -> float | None:
    """The wait a failure states, in seconds; None when it states none."""
    for kind, read in _RETRY_AFTER_READERS:
        if isinstance(value, kind):
            return read(value, now)
    return None

_RETRY_RANDOM = random.SystemRandom()

def _retry_available_at(
    state: str,
    attempts: int,
    now: datetime,
    retry_after: float | datetime | str | None = None,
) -> datetime:
    """A task going back to ready waits a full-jitter backoff, as the legacy queue's do.

    A wait the failure states — a provider's Retry-After — is a floor under the
    backoff, so the task comes back at the later of the two. See
    `docs/research/2026-09-14-the-small-integrity-gaps.md` and
    `docs/research/2026-09-17-the-adopted-queue-waits-as-long-as-it-was-told.md`.
    """
    if state != "ready":
        return now
    ceiling = min(DEFAULTS.retry_cap_seconds, DEFAULTS.retry_base_seconds * (2 ** max(0, attempts - 1)))
    stated = _retry_after_seconds(retry_after, now)
    delay = max(_RETRY_RANDOM.uniform(0, ceiling), stated or 0.0)
    return now + timedelta(seconds=delay)
