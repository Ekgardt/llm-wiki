"""Reserve retry-stable operation IDs for rate-limited capture breadcrumbs."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime

StateMutator = Callable[[dict], None]
StateUpdate = Callable[[StateMutator], object]


def _timestamp(entry: object) -> str | None:
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    value = entry.get("completed_at") or entry.get("reserved_at")
    return value if isinstance(value, str) else None


def _operation_id(prefix: str, key: str, source_event_id: str | None) -> str:
    occurrence = source_event_id or uuid.uuid4().hex
    digest = hashlib.sha256(f"{prefix}\0{key}\0{occurrence}".encode()).hexdigest()
    return f"{prefix}:{digest}"


def _count_dropped_write(error: BaseException) -> None:
    """A lost race is not a hook failure, but it is a dropped write: count it."""
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error

    record_capture_failure("capture_operation_state", describe_error(error), error=error)


def _fallback_operation_id(
    prefix: str, key: str, source_event_id: str | None
) -> str:
    occurrence = source_event_id or uuid.uuid4().hex
    digest = hashlib.sha256(f"{prefix}\0{key}\0{occurrence}".encode()).hexdigest()
    return f"{prefix}:fallback:{digest}"


def _is_recent(previous: str | None, now: datetime, rate_limit_seconds: int) -> bool:
    if previous is None:
        return False
    try:
        return (now - datetime.fromisoformat(previous)).total_seconds() < rate_limit_seconds
    except (TypeError, ValueError):
        return False


def _same_event(existing: dict, source_event_id: str | None) -> bool:
    return source_event_id is not None and existing.get("source_event_id") == source_event_id


def _pending_retry(existing: dict, source_event_id: str | None, recent: bool) -> bool:
    return existing.get("status") == "pending" and source_event_id is None and recent


def _replayed_operation(
    existing: object, source_event_id: str | None, recent: bool
) -> str | None:
    """The operation an existing reservation already answers, or None."""
    if not isinstance(existing, dict):
        return None
    operation = existing.get("operation_id")
    if not isinstance(operation, str):
        return None
    if _same_event(existing, source_event_id) or _pending_retry(existing, source_event_id, recent):
        return operation
    return None


def _trim_entries(state: dict, namespace: str, entries: dict, max_entries: int) -> None:
    if len(entries) <= max_entries:
        return
    newest = sorted(entries.items(), key=lambda item: _timestamp(item[1]) or "", reverse=True)
    state[namespace] = dict(newest[:max_entries])


class _Claim:
    """One reservation attempt; `mutate` runs under the state lock."""

    def __init__(self, namespace: str, key: str, prefix: str, source_event_id: str | None,
                 rate_limit_seconds: int, max_entries: int, now: datetime) -> None:
        self.namespace, self.key, self.prefix = namespace, key, prefix
        self.source_event_id = source_event_id
        self.rate_limit_seconds, self.max_entries, self.now = rate_limit_seconds, max_entries, now
        self.claimed: str | None = None
        self.observed = False

    def mutate(self, state: dict) -> None:
        self.observed = True
        entries = state.setdefault(self.namespace, {})
        existing = entries.get(self.key)
        recent = _is_recent(_timestamp(existing), self.now, self.rate_limit_seconds)
        replayed = _replayed_operation(existing, self.source_event_id, recent)
        if replayed is not None:
            self.claimed = replayed
            return
        if recent:
            return
        self._reserve(state, entries)

    def _reserve(self, state: dict, entries: dict) -> None:
        self.claimed = _operation_id(self.prefix, self.key, self.source_event_id)
        entries[self.key] = {
            "operation_id": self.claimed,
            "reserved_at": self.now.isoformat(timespec="seconds"),
            "source_event_id": self.source_event_id,
            "status": "pending",
        }
        _trim_entries(state, self.namespace, entries, self.max_entries)


def claim_operation(
    update: StateUpdate,
    *,
    namespace: str,
    key: str,
    prefix: str,
    source_event_id: str | None,
    rate_limit_seconds: int,
    max_entries: int,
    now: datetime,
) -> str | None:
    """Reserve an operation before append, or return None for a recent new event."""
    claim = _Claim(namespace, key, prefix, source_event_id, rate_limit_seconds, max_entries, now)
    try:
        update(claim.mutate)
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_write(exc)
        return _fallback_operation_id(prefix, key, source_event_id)
    if not claim.observed:
        return _fallback_operation_id(prefix, key, source_event_id)
    return claim.claimed


def complete_operation(
    update: StateUpdate,
    *,
    namespace: str,
    key: str,
    operation_id: str,
    now: datetime,
) -> None:
    """Mark a reserved operation committed without moving an existing completion time."""
    def mutate(state: dict) -> None:
        entry = state.get(namespace, {}).get(key)
        if (
            not isinstance(entry, dict)
            or entry.get("operation_id") != operation_id
            or entry.get("status") == "committed"
        ):
            return
        entry["status"] = "committed"
        entry["completed_at"] = now.isoformat(timespec="seconds")

    try:
        update(mutate)
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_write(exc)
