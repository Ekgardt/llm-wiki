"""The canonical capture payload a hook event is reduced to."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from event_envelope import EventEnvelope
from llm_wiki.integration_settings import CHECKPOINT_SIGNAL_FIELDS


def _tool_capture_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": payload["tool_name"],
        "tool_input": {"filePath": payload["target"], "command": payload["target"]},
    }


def _prompt_capture_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"prompt": payload["prompt"], "transcript_path": payload.get("transcript_path")}


def _lifecycle_capture_fields(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "reason": payload.get("reason"),
        "transcript_path": payload.get("transcript_path"),
    }
    if event_type == "pre_compact":
        fields["trigger"] = payload.get("reason")
    return fields


def _event_capture_fields(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    builders = {
        "post_tool_use": _tool_capture_fields,
        "user_prompt": _prompt_capture_fields,
    }
    builder = builders.get(event_type)
    if builder is not None:
        return builder(payload)
    return _lifecycle_capture_fields(event_type, payload)


def _copy_capture_occurrence(common: dict[str, Any], payload: Mapping[str, Any]) -> None:
    if "occurrence_id" in payload:
        common["occurrence_id"] = payload["occurrence_id"]


def _copy_capture_signals(common: dict[str, Any], payload: Mapping[str, Any]) -> None:
    for name in CHECKPOINT_SIGNAL_FIELDS | {"host_progress_signals"}:
        if name in payload:
            common[name] = payload[name]


def canonical_capture_payload(envelope: EventEnvelope) -> dict[str, Any]:
    payload = envelope.to_dict()["payload"]
    common = {
        "session_id": envelope.session,
        "cwd": envelope.worktree,
        "agent": envelope.agent,
        "severity": envelope.severity,
        "parent_event_id": envelope.parent_event_id,
        "event_id": envelope.event_id,
    }
    _copy_capture_occurrence(common, payload)
    common.update(_event_capture_fields(envelope.event_type, payload))
    _copy_capture_signals(common, payload)
    return common

