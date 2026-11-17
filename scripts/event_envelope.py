"""Native physical-frame readers and compatible event-construction exports."""
from __future__ import annotations

import json
from typing import Any

from llm_wiki.event_envelope_values import AGENT_PATTERNS as AGENT_PATTERNS
from llm_wiki.event_envelope_values import ALLOWED_EVENT_TYPES as ALLOWED_EVENT_TYPES
from llm_wiki.event_envelope_values import (
    BOOLEAN_CHECKPOINT_SIGNALS as BOOLEAN_CHECKPOINT_SIGNALS,
)
from llm_wiki.event_envelope_values import REQUIRED_PAYLOAD_FIELDS as REQUIRED_PAYLOAD_FIELDS
from llm_wiki.event_envelope_values import SCHEMA_VERSION as SCHEMA_VERSION
from llm_wiki.event_envelope_values import EventEnvelope as EventEnvelope
from llm_wiki.event_envelope_values import booleans_are_valid as booleans_are_valid
from llm_wiki.event_envelope_values import build_event_envelope as build_event_envelope
from llm_wiki.event_envelope_values import canonical as canonical
from llm_wiki.event_envelope_values import canonical_agent as canonical_agent
from llm_wiki.event_envelope_values import declared_occurrence as declared_occurrence
from llm_wiki.event_envelope_values import event_identity as event_identity
from llm_wiki.event_envelope_values import field_is_valid as field_is_valid
from llm_wiki.event_envelope_values import freeze as freeze
from llm_wiki.event_envelope_values import percent_is_valid as percent_is_valid
from llm_wiki.event_envelope_values import (
    redact_envelope_value as redact_envelope_value,
)
from llm_wiki.event_envelope_values import redacted_key as redacted_key
from llm_wiki.event_envelope_values import redacted_mapping as redacted_mapping
from llm_wiki.event_envelope_values import redacted_other as redacted_other
from llm_wiki.event_envelope_values import redacted_payload as redacted_payload
from llm_wiki.event_envelope_values import redacted_source_fields as redacted_source_fields
from llm_wiki.event_envelope_values import require_payload_fields as require_payload_fields
from llm_wiki.event_envelope_values import require_source_strings as require_source_strings
from llm_wiki.event_envelope_values import required_fields as required_fields
from llm_wiki.event_envelope_values import thaw as thaw
from llm_wiki.event_envelope_values import timestamp as timestamp
from llm_wiki.event_envelope_values import utc as utc
from llm_wiki.event_envelope_values import (
    validate_checkpoint_signals as validate_checkpoint_signals,
)


def native_user_text(text: str) -> str | None:
    """Decode a canonical captured user frame without changing its source bytes."""
    if not text.startswith("    {\""):
        return None
    encoded = text[4:].rstrip("\r\n")
    return native_selected_user_text(encoded)


def native_selected_user_text(encoded: str) -> str | None:
    event = _decoded_native_user_record(encoded)
    if event is None:
        return None
    return _native_record_user_text(event, encoded)


def _decoded_native_user_record(encoded: str) -> dict | None:
    try:
        event = json.loads(encoded)
    except json.JSONDecodeError:
        if _native_fragment_candidate(encoded):
            raise
        return None
    if not _native_record_candidate(event):
        return None
    return event


def _native_record_user_text(event: dict, encoded: str) -> str | None:
    _require_native_record(event, encoded)
    if event["event_type"] != "user_prompt":
        return None
    return event["payload"]["prompt"]


def native_part_user_text(text: str, *, allow_fragment: bool = False) -> str | None:
    """Read a complete event inside its verified physical breadcrumb part."""
    if not text.startswith("    {\""):
        return None
    encoded_part = text[4:].rstrip("\r\n")
    record = _native_part_record(encoded_part, allow_fragment)
    if record is None:
        return None
    return _native_part_prompt(record, allow_fragment)


def _native_part_prompt(record: dict, allow_fragment: bool) -> str | None:
    encoded = record["text"]
    if record["index"] != 0:
        return None
    return _native_part_content(encoded, allow_fragment)


def native_encoding_candidate(encoded: str) -> bool:
    """Distinguish outer native identity from generic or nested JSON data."""
    try:
        event = json.loads(encoded)
    except json.JSONDecodeError:
        return _native_fragment_candidate(encoded)
    return _native_record_candidate(event)


def _native_record_candidate(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    actor_fields = {"agent", "session", "project", "worktree", "severity",
                    "parent_event_id", "source_event_id", "payload"}
    return {"event_type", "schema_version"}.issubset(event) or actor_fields.issubset(event)


def _native_fragment_candidate(encoded: str) -> bool:
    """Keep recognition of incomplete canonical native parts separate."""
    return encoded.startswith('{"agent":') or (encoded.startswith("{") and
            '"event_type":' in encoded and '"schema_version":' in encoded)


def _native_part_record(encoded: str, allow_fragment: bool) -> dict | None:
    from breadcrumb_protocol import read_part

    parsed = _physical_json_object(encoded, allow_fragment)
    if parsed is None or parsed.get("schema_version") != "breadcrumb-part/v1":
        return None
    return read_part(encoded.encode("utf-8"))


def _physical_json_object(encoded: str, allow_fragment: bool) -> dict | None:
    try:
        return json.loads(encoded)
    except json.JSONDecodeError:
        if allow_fragment:
            return None
        raise


def _native_part_content(encoded: str, allow_fragment: bool) -> str | None:
    try:
        return native_selected_user_text(encoded)
    except json.JSONDecodeError as exc:
        if allow_fragment:
            return None
        raise ValueError("native frame requires complete physical evidence; remains pending") from exc


def native_physical_user_text(text: str, ancestry: tuple[str, ...], *, allow_fragment: bool = False) -> str | None:
    if ancestry and ancestry[-1].endswith("Captured event"):
        return native_user_text(text)
    if ancestry and ancestry[-1] == "Integrity record":
        return native_part_user_text(text, allow_fragment=allow_fragment)
    return None


def _require_native_record(event: Any, encoded: str) -> None:
    fields = {"schema_version", "event_type", "payload", "agent", "session",
              "project", "worktree", "severity", "parent_event_id", "source_event_id"}
    if not isinstance(event, dict) or set(event) != fields:
        raise ValueError("invalid native captured event fields")
    _require_native_encoding(event, encoded)
    require_payload_fields(event["payload"], required_fields(event["event_type"], event["payload"]))
    require_source_strings(tuple(event[name] for name in fields - {"schema_version", "event_type", "payload"}))


def _require_native_encoding(event: dict, encoded: str) -> None:
    if event["schema_version"] != SCHEMA_VERSION or canonical(event) != encoded:
        raise ValueError("invalid native captured event version or encoding")


def native_frame_heading(ancestry: tuple[str, ...]) -> bool:
    return bool(ancestry and (ancestry[-1].endswith("Captured event") or ancestry[-1] == "Integrity record"))
