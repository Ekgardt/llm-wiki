"""Lease-time values and role timing policy; no clocks, SQL or ownership mutation."""
from __future__ import annotations

from datetime import UTC, datetime

from iso_time import utc_text
from llm_wiki.ownership_contracts import OperationalOwnershipError, OwnerLease, OwnerRole

LONG_LEASE_ROLES = frozenset(
    {"queue-worker", "compile", "nightly", "weekly", "queue-operator", "repair"}
)


def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("ownership clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    """Always six fractional digits, so the text order SQL compares is time order.

    `isoformat()` drops the fraction on a whole second, and `Z` sorts after `.`:
    `…:00Z` compared greater than `…:00.500000Z`. The reader takes both shapes.
    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    return utc_text(as_utc(value))


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise OperationalOwnershipError("owner_record_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperationalOwnershipError("owner_record_invalid") from exc
    return as_utc(parsed)


def timing(role: OwnerRole) -> tuple[int, int]:
    return (120, 40) if role in LONG_LEASE_ROLES else (30, 10)


def require_lease_timing(lease: OwnerLease, role: OwnerRole) -> None:
    if (lease.ttl_seconds, lease.heartbeat_seconds) != timing(role):
        raise ValueError("lease timing does not match its role")

# Preserve historical serialized helper identities via the original aliases.
as_utc.__module__ = "operational_ownership"
timestamp.__module__ = "operational_ownership"
parse_timestamp.__module__ = "operational_ownership"
timing.__module__ = "operational_ownership"
require_lease_timing.__module__ = "operational_ownership"
