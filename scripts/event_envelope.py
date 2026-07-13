"""Versioned, immutable event envelopes for native capture adapters."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = "1.0"
ALLOWED_EVENT_TYPES = frozenset(
    {"session_start", "session_end", "pre_compact", "user_prompt", "post_tool_use"}
)
_REQUIRED_PAYLOAD_FIELDS = {
    "session_start": {"reason": (str, type(None))},
    "session_end": {
        "reason": (str, type(None)),
        "transcript_path": (str, type(None)),
    },
    "pre_compact": {
        "reason": (str, type(None)),
        "transcript_path": (str, type(None)),
    },
    "user_prompt": {"prompt": str},
    "post_tool_use": {"tool_name": str, "target": str},
}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _redact(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("event payload mapping keys must be strings")
            safe_key = redact(key)
            if not isinstance(safe_key, str):
                raise ValueError("event payload mapping keys must be strings")
            if safe_key in redacted:
                raise ValueError("event payload keys collide after redaction")
            redacted[safe_key] = _redact(item, redact)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item, redact) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must contain strict JSON values") from exc


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    occurred_at: datetime
    captured_at: datetime
    agent: str | None
    session: str | None
    project: str | None
    worktree: str | None
    severity: str | None
    schema_version: str
    content_hash: str
    parent_event_id: str | None
    source_event_id: str | None
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": _timestamp(self.occurred_at),
            "captured_at": _timestamp(self.captured_at),
            "agent": self.agent,
            "session": self.session,
            "project": self.project,
            "worktree": self.worktree,
            "severity": self.severity,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "parent_event_id": self.parent_event_id,
            "source_event_id": self.source_event_id,
            "payload": _thaw(self.payload),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())


def build_event_envelope(
    *,
    event_type: str,
    payload: Mapping[str, Any],
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
    agent: str | None = None,
    session: str | None = None,
    project: str | None = None,
    worktree: str | None = None,
    severity: str | None = None,
    parent_event_id: str | None = None,
    source_event_id: str | None = None,
    redact: Callable[[str], str] | None = None,
) -> EventEnvelope:
    """Validate and construct one canonical event envelope."""
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError("invalid event type")
    if not isinstance(payload, Mapping):
        raise ValueError("invalid event payload")
    required = _REQUIRED_PAYLOAD_FIELDS[event_type]
    if any(not isinstance(payload.get(name), expected) for name, expected in required.items()):
        raise ValueError("invalid event payload")
    source_fields = (
        agent,
        session,
        project,
        worktree,
        severity,
        parent_event_id,
        source_event_id,
    )
    if any(value is not None and not isinstance(value, str) for value in source_fields):
        raise ValueError("event source fields must be strings or null")

    now = datetime.now(timezone.utc)
    occurred = _utc(occurred_at or now)
    captured = _utc(captured_at or now)
    redact_value = redact or (lambda value: value)
    safe_payload = _redact(payload, redact_value)
    if any(
        not isinstance(safe_payload.get(name), expected)
        for name, expected in required.items()
    ):
        raise ValueError("invalid event payload")
    payload_dict = dict(safe_payload)
    safe_source_fields = tuple(
        redact_value(value) if value is not None else None for value in source_fields
    )
    if any(value is not None and not isinstance(value, str) for value in safe_source_fields):
        raise ValueError("event source fields must be strings or null")
    (
        safe_agent,
        safe_session,
        safe_project,
        safe_worktree,
        safe_severity,
        safe_parent,
        safe_source_event,
    ) = safe_source_fields
    payload_json = _canonical(payload_dict)
    content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    identity = {
        "event_type": event_type,
        "occurred_at": _timestamp(occurred) if occurred_at is not None else None,
        "agent": safe_agent,
        "session": safe_session,
        "project": safe_project,
        "worktree": safe_worktree,
        "severity": safe_severity,
        "schema_version": SCHEMA_VERSION,
        "content_hash": content_hash,
        "parent_event_id": safe_parent,
        "source_event_id": safe_source_event,
        "payload": payload_dict,
    }
    event_id = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred,
        captured_at=captured,
        agent=safe_agent,
        session=safe_session,
        project=safe_project,
        worktree=safe_worktree,
        severity=safe_severity,
        schema_version=SCHEMA_VERSION,
        content_hash=content_hash,
        parent_event_id=safe_parent,
        source_event_id=safe_source_event,
        payload=_freeze(payload_dict),
    )
