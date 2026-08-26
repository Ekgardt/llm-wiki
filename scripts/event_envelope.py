"""Versioned, immutable event envelopes for native capture adapters."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = "1.0"
AGENT_PATTERNS = (
    (re.compile(r"\bopencode\b", re.IGNORECASE), "opencode"),
    (re.compile(r"\bcodex\b", re.IGNORECASE), "codex"),
    (re.compile(r"\bclaude(?:\s+code)?\b", re.IGNORECASE), "claude"),
)
ALLOWED_EVENT_TYPES = frozenset(
    {"session_start", "session_end", "pre_compact", "stop", "user_prompt", "post_tool_use"}
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
    "stop": {"reason": (str, type(None))},
    "user_prompt": {"prompt": str},
    "post_tool_use": {"tool_name": str, "target": str},
}
_BOOLEAN_CHECKPOINT_SIGNALS = frozenset(
    {
        "dirty",
        "changed",
        "significant",
        "decision",
        "correction",
        "blocker_opened",
        "blocker_closed",
        "task_completed",
        "task_cancelled",
        "ownership_transferred",
        "significant_failure",
        "public_contract_changed",
        "test_result_changed",
        "compaction_confirmed",
        "host_progress_signals",
    }
)


def canonical_agent(text: str) -> str:
    """Return one stable, low-cardinality software-agent identity."""
    return next(
        (name for pattern, name in AGENT_PATTERNS if pattern.search(str(text))),
        "unknown",
    )


def _booleans_are_valid(payload: Mapping[str, Any]) -> bool:
    present = (name for name in _BOOLEAN_CHECKPOINT_SIGNALS if name in payload)
    return all(isinstance(payload[name], bool) for name in present)


def _percent_is_valid(payload: Mapping[str, Any]) -> bool:
    if "token_percent" not in payload:
        return True
    percent = payload["token_percent"]
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return False
    return 0 <= percent <= 100


def _field_is_valid(payload: Mapping[str, Any], name: str, expected: type) -> bool:
    return name not in payload or isinstance(payload[name], expected)


def _validate_checkpoint_signals(payload: Mapping[str, Any]) -> None:
    verdicts = (
        _booleans_are_valid(payload),
        _percent_is_valid(payload),
        _field_is_valid(payload, "checkpoint_type", str),
        _field_is_valid(payload, "project_delta", Mapping),
    )
    if not all(verdicts):
        raise ValueError("invalid checkpoint signal")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _redacted_key(key: object, redact: Callable[[str], str]) -> str:
    if not isinstance(key, str):
        raise ValueError("event payload mapping keys must be strings")
    safe_key = redact(key)
    if not isinstance(safe_key, str):
        raise ValueError("event payload mapping keys must be strings")
    return safe_key


def _redacted_mapping(value: Mapping[Any, Any], redact: Callable[[str], str]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = _redacted_key(key, redact)
        if safe_key in redacted:
            raise ValueError("event payload keys collide after redaction")
        redacted[safe_key] = _redact(item, redact)
    return redacted


def _redacted_other(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, (list, tuple)):
        return [_redact(item, redact) for item in value]
    return value


def _redact(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return _redacted_mapping(value, redact)
    return _redacted_other(value, redact)


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


def _required_fields(event_type: str, payload: Mapping[str, Any]) -> Mapping[str, object]:
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError("invalid event type")
    if not isinstance(payload, Mapping):
        raise ValueError("invalid event payload")
    return _REQUIRED_PAYLOAD_FIELDS[event_type]


def _require_payload_fields(payload: Mapping[str, Any], required: Mapping[str, object]) -> None:
    if any(not isinstance(payload.get(name), expected) for name, expected in required.items()):
        raise ValueError("invalid event payload")


def _require_source_strings(fields: tuple[str | None, ...]) -> tuple[str | None, ...]:
    if any(value is not None and not isinstance(value, str) for value in fields):
        raise ValueError("event source fields must be strings or null")
    return fields


def _redacted_source_fields(
    fields: tuple[str | None, ...], redact_value: Callable[[str], str]
) -> tuple[str | None, ...]:
    safe = tuple(redact_value(value) if value is not None else None for value in fields)
    return _require_source_strings(safe)


def _redacted_payload(
    payload: Mapping[str, Any],
    required: Mapping[str, object],
    redact_value: Callable[[str], str],
) -> dict[str, Any]:
    safe_payload = _redact(payload, redact_value)
    _require_payload_fields(safe_payload, required)
    payload_dict = dict(safe_payload)
    _validate_checkpoint_signals(payload_dict)
    return payload_dict


def _declared_occurrence(occurred: datetime, occurred_at: datetime | None) -> str | None:
    if occurred_at is None:
        return None
    return _timestamp(occurred)


def _event_identity(
    event_type: str,
    declared_occurrence: str | None,
    sources: tuple[str | None, ...],
    content_hash: str,
    payload_dict: Mapping[str, Any],
) -> dict[str, Any]:
    agent, session, project, worktree, severity, parent, source_event = sources
    return {
        "event_type": event_type,
        "occurred_at": declared_occurrence,
        "agent": agent,
        "session": session,
        "project": project,
        "worktree": worktree,
        "severity": severity,
        "schema_version": SCHEMA_VERSION,
        "content_hash": content_hash,
        "parent_event_id": parent,
        "source_event_id": source_event,
        "payload": payload_dict,
    }


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
    required = _required_fields(event_type, payload)
    _require_payload_fields(payload, required)
    _validate_checkpoint_signals(payload)
    source_fields = _require_source_strings(
        (agent, session, project, worktree, severity, parent_event_id, source_event_id)
    )
    now = datetime.now(timezone.utc)
    occurred = _utc(occurred_at or now)
    captured = _utc(captured_at or now)
    redact_value = redact or (lambda value: value)
    payload_dict = _redacted_payload(payload, required, redact_value)
    sources = _redacted_source_fields(source_fields, redact_value)
    content_hash = hashlib.sha256(_canonical(payload_dict).encode("utf-8")).hexdigest()
    identity = _event_identity(
        event_type,
        _declared_occurrence(occurred, occurred_at),
        sources,
        content_hash,
        payload_dict,
    )
    return EventEnvelope(
        event_id=hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest(),
        event_type=event_type,
        occurred_at=occurred,
        captured_at=captured,
        agent=sources[0],
        session=sources[1],
        project=sources[2],
        worktree=sources[3],
        severity=sources[4],
        schema_version=SCHEMA_VERSION,
        content_hash=content_hash,
        parent_event_id=sources[5],
        source_event_id=sources[6],
        payload=_freeze(payload_dict),
    )
