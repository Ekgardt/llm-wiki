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


def _fallback_operation_id(
    prefix: str, key: str, source_event_id: str | None
) -> str:
    occurrence = source_event_id or uuid.uuid4().hex
    digest = hashlib.sha256(f"{prefix}\0{key}\0{occurrence}".encode()).hexdigest()
    return f"{prefix}:fallback:{digest}"


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
    claimed: str | None = None
    observed = False
    now_text = now.isoformat(timespec="seconds")

    def mutate(state: dict) -> None:
        nonlocal claimed, observed
        observed = True
        entries = state.setdefault(namespace, {})
        existing = entries.get(key)
        previous = _timestamp(existing)
        recent = False
        if previous is not None:
            try:
                recent = (
                    now - datetime.fromisoformat(previous)
                ).total_seconds() < rate_limit_seconds
            except (TypeError, ValueError):
                pass
        if isinstance(existing, dict):
            operation = existing.get("operation_id")
            existing_source = existing.get("source_event_id")
            status = existing.get("status")
            if isinstance(operation, str):
                if source_event_id is not None and existing_source == source_event_id:
                    claimed = operation
                    return
                if status == "pending" and source_event_id is None and recent:
                    claimed = operation
                    return
        if recent:
            return

        claimed = _operation_id(prefix, key, source_event_id)
        entries[key] = {
            "operation_id": claimed,
            "reserved_at": now_text,
            "source_event_id": source_event_id,
            "status": "pending",
        }
        if len(entries) > max_entries:
            newest = sorted(
                entries.items(),
                key=lambda item: _timestamp(item[1]) or "",
                reverse=True,
            )[:max_entries]
            state[namespace] = dict(newest)

    try:
        update(mutate)
    except Exception:  # noqa: BLE001
        return _fallback_operation_id(prefix, key, source_event_id)
    if not observed:
        return _fallback_operation_id(prefix, key, source_event_id)
    return claimed


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
    except Exception:  # noqa: BLE001
        pass
