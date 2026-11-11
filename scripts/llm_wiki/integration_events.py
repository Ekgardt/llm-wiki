"""Normalizing agent hook events into one canonical event shape."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import project_checkpoint
from event_envelope import EventEnvelope, build_event_envelope
from llm_wiki.integration_settings import (
    CHECKPOINT_SIGNAL_FIELDS,
    EVENTS,
    MAX_TRANSCRIPT_TEXT_CHARS,
    SOURCES,
)
from secret_redact import redact_secrets


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str)), None)


def _safe_string(value: str | None) -> str | None:
    return redact_secrets(value) if value is not None else None


def _source_event_id(raw: Mapping[str, Any]) -> str | None:
    candidates = (
        raw.get("source_event_id"),
        raw.get("occurrence_id"),
        raw.get("event_id"),
        raw.get("eventId"),
        raw.get("tool_use_id"),
        raw.get("toolCallID"),
        raw.get("callID"),
    )
    return next((value for value in candidates if isinstance(value, str) and value), None)


def session(source: str, raw: Mapping[str, Any]) -> str | None:
    # OpenCode's `session.created` sends `{info: Session}` (SDK types, 2026-09-25).
    info = raw.get("info", raw.get("sessionInfo"))
    nested = info.get("id") if isinstance(info, Mapping) else None
    if source == "opencode":
        return _first_string(nested, raw.get("sessionId"), raw.get("sessionID"))
    return project_checkpoint.string(raw.get("session_id"))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid integration event")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid integration event") from exc


_TOOL_NAMES = {
    "edit": "Edit",
    "write": "Write",
    "multi_edit": "MultiEdit",
    "multiedit": "MultiEdit",
    "notebook_edit": "NotebookEdit",
    "notebookedit": "NotebookEdit",
    "bash": "Bash",
    "shell": "Bash",
    # Codex edits files through `apply_patch`; its patch text is the command.
    "apply_patch": "Edit",
}


_PATCHED_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def tool_payload(source: str, raw: Mapping[str, Any]) -> dict[str, str]:
    raw_name = _first_string(raw.get("tool_name"), raw.get("tool")) or ""
    tool_input = raw.get("tool_input") if source != "opencode" else _opencode_arguments(raw)
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    target = (
        _first_string(
            tool_input.get("filePath"),
            tool_input.get("file_path"),
            tool_input.get("command"),
            raw.get("target"),
        )
        or ""
    )
    return {
        "tool_name": _TOOL_NAMES.get(raw_name.lower(), raw_name),
        "target": _tool_target(raw_name, target),
    }


def _opencode_arguments(raw: Mapping[str, Any]) -> object:
    """`tool.execute.after` sends the call's arguments as `args` (SDK types, 2026-09-25)."""
    arguments = raw.get("args")
    if isinstance(arguments, Mapping):
        return arguments
    arguments = raw.get("input")
    if isinstance(arguments, Mapping):
        return arguments
    return raw.get("tool_input")


def _tool_target(raw_name: str, target: str) -> str:
    """A patch is named by the first file it touches, not by its text."""
    if raw_name.lower() != "apply_patch":
        return target
    match = _PATCHED_FILE.search(target)
    return match.group(1).strip() if match else target


DELTA_OPERATION_NAMES = tuple(f"{name}_operations" for name in project_checkpoint.DELTA_SCALAR_NAMES)


def _delta_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid project delta")
    if set(value) != {"id", "action", "value"}:
        raise ValueError("invalid project delta")
    return value


def _delta_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid project delta")
    if not 1 <= len(value) <= 256:
        raise ValueError("invalid project delta")
    return value


def _delta_action(value: object) -> str:
    if value not in {"upsert", "close"}:
        raise ValueError("invalid project delta")
    return str(value)


def _delta_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid project delta")
    if len(value) > 4096:
        raise ValueError("invalid project delta")
    return value


def _canonical_delta_operation(value: object) -> dict[str, str]:
    operation = _delta_mapping(value)
    return {
        "id": _delta_id(operation.get("id")),
        "action": _delta_action(operation.get("action")),
        "value": _delta_text(operation.get("value")),
    }


def _validate_delta_names(value: Mapping[str, Any]) -> None:
    allowed = project_checkpoint.DELTA_SCALAR_NAMES + project_checkpoint.DELTA_LIST_NAMES + DELTA_OPERATION_NAMES + ("legacy_context",)
    if set(value) - set(allowed):
        raise ValueError("invalid project delta")


def _copy_scalar_deltas(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    for name in project_checkpoint.DELTA_SCALAR_NAMES:
        if name in value:
            canonical[name] = _canonical_delta_operation(value[name])


def _canonical_delta_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("invalid project delta")
    if len(value) > 10_000:
        raise ValueError("invalid project delta")
    return [_canonical_delta_operation(item) for item in value]


def _copy_delta_lists(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    for name in DELTA_OPERATION_NAMES + project_checkpoint.DELTA_LIST_NAMES:
        if name in value:
            canonical[name] = _canonical_delta_list(value[name])


def _copy_legacy_context(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    if "legacy_context" not in value:
        return
    legacy_context = value["legacy_context"]
    if not isinstance(legacy_context, str) or len(legacy_context) > 16384:
        raise ValueError("invalid project delta")
    canonical["legacy_context"] = legacy_context


def _canonical_project_delta(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid project delta")
    _validate_delta_names(value)
    canonical = project_checkpoint.empty_delta()
    _copy_scalar_deltas(value, canonical)
    _copy_delta_lists(value, canonical)
    _copy_legacy_context(value, canonical)
    return canonical


def _session_start_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"reason": _first_string(raw.get("reason"), raw.get("trigger"), raw.get("source"))}


def _stop_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"reason": _first_string(raw.get("reason"), raw.get("trigger"))}


def _transcript_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "reason": _first_string(raw.get("reason"), raw.get("trigger")),
        "transcript_path": _first_string(
            raw.get("transcript_path"), raw.get("transcriptPath"), raw.get("transcript")
        ),
    }
    if "transcript_text" not in raw:
        return payload
    transcript_text = raw.get("transcript_text")
    if not isinstance(transcript_text, str) or len(transcript_text) > MAX_TRANSCRIPT_TEXT_CHARS:
        raise ValueError("invalid integration event")
    payload["transcript_text"] = transcript_text
    return payload


def _user_prompt_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The prompt, and the transcript the host named with it.

    The path is what the twentieth-prompt capture reads; without it that capture
    had nothing to read and counted an empty session. See
    `docs/research/2026-09-17-the-twentieth-prompt-captures-the-session.md`.
    """
    return {
        "prompt": project_checkpoint.string(raw.get("prompt")),
        "transcript_path": _first_string(raw.get("transcript_path"), raw.get("transcriptPath")),
    }


def _event_payload(source: str, event: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    builders = {
        "session_start": _session_start_payload,
        "stop": _stop_payload,
        "session_end": _transcript_payload,
        "pre_compact": _transcript_payload,
        "user_prompt": _user_prompt_payload,
    }
    builder = builders.get(event)
    if builder is None:
        return tool_payload(source, raw)
    return builder(raw)


def _copy_checkpoint_signals(payload: dict[str, Any], raw: Mapping[str, Any]) -> None:
    for name in CHECKPOINT_SIGNAL_FIELDS:
        if name in raw:
            payload[name] = raw[name]


def _normalize_payload_delta(payload: dict[str, Any]) -> None:
    if "project_delta" in payload:
        payload["project_delta"] = _canonical_project_delta(payload["project_delta"])


def _copy_occurrence(payload: dict[str, Any], event: str, raw: Mapping[str, Any]) -> None:
    if event in EVENTS and isinstance(raw.get("occurrence_id"), str):
        payload["occurrence_id"] = raw["occurrence_id"]


def _apply_tool_mutation_defaults(payload: dict[str, Any], event: str) -> None:
    if event != "post_tool_use":
        return
    if payload["tool_name"] not in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        return
    payload.setdefault("changed", True)
    payload.setdefault("dirty", True)
    payload.setdefault("significant", True)


def _apply_failure_default(payload: dict[str, Any], event: str) -> None:
    if event == "post_tool_use" and payload.get("checkpoint_type") == "significant_failure":
        payload["significant_failure"] = True


def _apply_compaction_default(payload: dict[str, Any], event: str) -> None:
    if event == "session_start" and payload.get("reason") == "compact":
        payload["compaction_confirmed"] = True


def normalize_event(
    source: str,
    event: str,
    raw: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> EventEnvelope:
    """Map one host-shaped event to the canonical, redacted envelope."""
    if source not in SOURCES or event not in EVENTS or not isinstance(raw, Mapping):
        raise ValueError("invalid integration event")
    projected = raw
    payload = _event_payload(source, event, projected)
    _copy_checkpoint_signals(payload, projected)
    _normalize_payload_delta(payload)
    _copy_occurrence(payload, event, projected)
    _apply_tool_mutation_defaults(payload, event)
    _apply_failure_default(payload, event)
    _apply_compaction_default(payload, event)

    source_time = occurred_at or _parse_timestamp(projected.get("timestamp"))
    _breadcrumb_time_origin(payload, event, source_time)
    return build_event_envelope(
        event_type=event,
        payload=payload,
        occurred_at=source_time,
        captured_at=captured_at,
        agent=source,
        session=_safe_string(session(source, projected)),
        project=_safe_string(project_checkpoint.string(projected.get("project"))),
        worktree=_safe_string(
            _first_string(
                projected.get("cwd"),
                projected.get("directory"),
                projected.get("projectRoot"),
            )
        ),
        severity=_safe_string(project_checkpoint.string(projected.get("severity"))),
        parent_event_id=_safe_string(project_checkpoint.string(projected.get("parent_event_id"))),
        source_event_id=_safe_string(_source_event_id(projected)),
        redact=redact_secrets,
    )


def _breadcrumb_time_origin(payload: dict, event: str, source_time: datetime | None) -> None:
    if event in {"user_prompt", "post_tool_use"}:
        payload["capture_time_origin"] = "acceptance" if source_time is None else "host"


def normalize_occurrence_event(
    source: str,
    event: str,
    raw: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> EventEnvelope:
    """Assign missing occurrence identity once at the outer adapter boundary."""
    normalized_raw = raw
    if event in EVENTS and _source_event_id(raw) is None:
        normalized_raw = dict(raw)
        normalized_raw["occurrence_id"] = str(uuid.uuid4())
    return normalize_event(
        source,
        event,
        normalized_raw,
        occurred_at=occurred_at,
        captured_at=captured_at,
    )

