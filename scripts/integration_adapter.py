"""Normalize native host lifecycle events before existing capture pipelines."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from event_envelope import EventEnvelope, build_event_envelope
from maybe_compile import spawn_compile_if_idle
from memory_state import ROOT, STATE_ROOT, spawn_detached, update_state
from project_journal import (
    SESSION_START_RECOVERY_SECONDS,
    CheckpointDecision,
    CheckpointReducer,
    ProjectStore,
    recover_project_handoff,
)
from secret_redact import redact_secrets
from session_start_project_state import _compute_slug

SCRIPTS_DIR = Path(__file__).resolve().parent
DELEGATE_TIMEOUT_SECONDS = 10
MAINTENANCE_DRAIN_TIMEOUT_SECONDS = 600
MAX_TRANSCRIPT_TEXT_CHARS = 8000
MAX_CHECKPOINT_ERROR_CHARS = 500
MAX_STDIN_BYTES = 65_536
MAX_HOST_STRING_CHARS = 16_384
MAX_HOST_ID_CHARS = 512
MAX_WORKSPACE_ROOTS = 16
MAX_HOST_INDEX = 1_000_000_000
TRANSIENT_CREATE_ATTEMPTS = 10
PENDING_CLAIM_SECONDS = 30.0
MAX_CAPTURE_INTENT_BYTES = 1024 * 1024
MAX_CAPTURE_EVIDENCE_BYTES = 900 * 1024
CAPTURE_HANDLER_VERSION = 1
IDE_SOURCES = frozenset({"cursor", "antigravity"})
SOURCES = frozenset({"claude", "opencode", "codex", *IDE_SOURCES})
EVENTS = frozenset(
    {"session_start", "session_end", "pre_compact", "stop", "user_prompt", "post_tool_use"}
)


def build_session_start_context() -> Sequence[Any]:
    """Build structured context without loading SessionStart on unrelated commands."""
    from session_start_context import build_context_items

    return build_context_items()


OCCURRENCE_EVENTS = EVENTS - {"user_prompt"}
CHECKPOINT_SIGNAL_FIELDS = frozenset(
    {
        "checkpoint_type",
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
        "token_percent",
        "compaction_confirmed",
        "project_delta",
        "branch",
    }
)
DELEGATES = frozenset(
    {
        "session_start_context.py",
        "session_start_project_state.py",
        "precompact_capture.py",
        "session_end_capture.py",
        "session_end_project_tag.py",
        "user_prompt_capture.py",
        "post_tool_capture.py",
        "heartbeat_record.py",
        "feedback_capture.py",
    }
)
CAPTURE_DELEGATES = {
    "pre_compact": "precompact_capture.py",
    "session_end": "session_end_capture.py",
}


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str)), None)


def _safe_string(value: str | None) -> str | None:
    return redact_secrets(value) if value is not None else None


def _bounded_string(
    value: object,
    *,
    limit: int = MAX_HOST_STRING_CHARS,
    required: bool = False,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError("invalid integration event")
    return value


def _bounded_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid integration event")
    return value


def _bounded_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_WORKSPACE_ROOTS:
        raise ValueError("invalid integration event")
    return tuple(str(_bounded_string(item, required=True)) for item in value)


def _bounded_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_HOST_INDEX:
        raise ValueError("invalid integration event")
    return value


def _bounded_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("invalid integration event")
    return value


def _bounded_percent(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("invalid integration event")
    return value


def _first_bounded(
    raw: Mapping[str, Any],
    *names: str,
    limit: int = MAX_HOST_STRING_CHARS,
) -> str | None:
    values = (_bounded_string(raw.get(name), limit=limit) for name in names)
    return next((value for value in values if value is not None), None)


def _single_workspace(roots: tuple[str, ...]) -> str | None:
    if len(roots) == 1:
        return roots[0]
    return None


def _host_base(
    raw: Mapping[str, Any],
    *,
    session_names: tuple[str, ...],
    roots_name: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    session = _first_bounded(raw, *session_names, limit=MAX_HOST_ID_CHARS)
    worktree = cwd or _single_workspace(_bounded_strings(raw.get(roots_name)))
    projected: dict[str, Any] = {"session_id": session}
    if worktree is not None:
        projected["cwd"] = worktree
    return projected


def _hashed_source_event_id(source: str, event: str, projected: Mapping[str, Any]) -> str:
    identity = {"source": source, "event": event, "fields": projected}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    safe_encoded = redact_secrets(encoded)
    return hashlib.sha256(safe_encoded.encode("utf-8")).hexdigest()


def _attach_source_event_id(
    source: str,
    event: str,
    raw: Mapping[str, Any],
    projected: dict[str, Any],
    exact_id: str | None,
) -> dict[str, Any]:
    source_id = _bounded_string(raw.get("source_event_id"), limit=MAX_HOST_ID_CHARS)
    if source_id is None:
        source_id = exact_id
    if source_id is None:
        source_id = _hashed_source_event_id(source, event, projected)
    projected["source_event_id"] = source_id
    return projected


def _cursor_session_start(raw: Mapping[str, Any]) -> dict[str, Any]:
    reason = _bounded_string(raw.get("source")) or "cursor-session-start"
    return {"reason": reason}


def _cursor_user_prompt(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"prompt": _bounded_string(raw.get("prompt"), required=True)}


def _cursor_post_tool(raw: Mapping[str, Any]) -> dict[str, Any]:
    tool_input = _bounded_mapping(raw.get("tool_input"))
    tool_name = _bounded_string(raw.get("tool_name"), limit=128, required=True)
    target = _first_bounded(tool_input, "file_path", "filePath", "command") or ""
    return {"tool_name": tool_name, "tool_input": {"file_path": target}}


def _cursor_pre_compact(raw: Mapping[str, Any]) -> dict[str, Any]:
    usage = _bounded_percent(raw.get("context_usage_percent"))
    payload: dict[str, Any] = {"reason": "cursor-pre-compact"}
    if usage is not None:
        payload["token_percent"] = usage
    return payload


def _cursor_stop(raw: Mapping[str, Any]) -> dict[str, Any]:
    reason = _bounded_string(raw.get("status")) or "cursor-stop"
    return {"reason": reason}


def _cursor_session_end(raw: Mapping[str, Any]) -> dict[str, Any]:
    _bounded_string(raw.get("transcript_path"))
    return {"reason": "cursor-session-end"}


def _project_cursor(event: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    builders = {
        "session_start": _cursor_session_start,
        "user_prompt": _cursor_user_prompt,
        "post_tool_use": _cursor_post_tool,
        "pre_compact": _cursor_pre_compact,
        "stop": _cursor_stop,
        "session_end": _cursor_session_end,
    }
    builder = builders.get(event)
    if builder is None:
        raise ValueError("invalid integration event")
    cwd = _bounded_string(raw.get("cwd"))
    projected = _host_base(
        raw,
        session_names=("conversation_id", "session_id"),
        roots_name="workspace_roots",
        cwd=cwd,
    )
    projected.update(builder(raw))
    id_name = "tool_use_id" if event == "post_tool_use" else "generation_id"
    exact_id = _bounded_string(raw.get(id_name), limit=MAX_HOST_ID_CHARS)
    return _attach_source_event_id("cursor", event, raw, projected, exact_id)


ANTIGRAVITY_TOOLS = {
    "write_to_file": ("Write", "TargetFile"),
    "replace_file_content": ("Edit", "TargetFile"),
    "multi_replace_file_content": ("MultiEdit", "TargetFile"),
    "run_command": ("Bash", "CommandLine"),
}


def _antigravity_session_start(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reason": "antigravity-initial-invocation",
        "host_invocation_num": _bounded_index(raw.get("invocationNum")),
    }


def _antigravity_post_tool(raw: Mapping[str, Any]) -> dict[str, Any]:
    tool_call = _bounded_mapping(raw.get("toolCall"))
    host_name = _bounded_string(tool_call.get("name"), limit=128, required=True)
    tool_spec = ANTIGRAVITY_TOOLS.get(str(host_name))
    if tool_spec is None:
        raise ValueError("invalid integration event")
    tool_input = _bounded_mapping(tool_call.get("args"))
    tool_name, target_name = tool_spec
    target = _bounded_string(tool_input.get(target_name), required=True)
    projected: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": {"file_path": target},
        "host_step_index": _bounded_index(raw.get("stepIdx")),
    }
    cwd = _bounded_string(tool_input.get("Cwd"))
    if cwd is not None:
        projected["cwd"] = cwd
    error = _bounded_string(raw.get("error"), limit=4096)
    if error:
        projected["checkpoint_type"] = "significant_failure"
    return projected


def _antigravity_stop(raw: Mapping[str, Any]) -> dict[str, Any]:
    reason = _bounded_string(raw.get("terminationReason")) or "antigravity-stop"
    return {
        "reason": reason,
        "host_execution_num": _bounded_index(raw.get("executionNum")),
    }


def _project_antigravity(event: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    builders = {
        "session_start": _antigravity_session_start,
        "post_tool_use": _antigravity_post_tool,
        "stop": _antigravity_stop,
        "session_end": _antigravity_stop,
    }
    builder = builders.get(event)
    if builder is None:
        raise ValueError("invalid integration event")
    projected = _host_base(
        raw,
        session_names=("conversationId",),
        roots_name="workspacePaths",
    )
    projected.update(builder(raw))
    return _attach_source_event_id("antigravity", event, raw, projected, None)


def _project_source_payload(source: str, event: str, raw: Mapping[str, Any]) -> Mapping[str, Any]:
    projectors = {"cursor": _project_cursor, "antigravity": _project_antigravity}
    projector = projectors.get(source)
    if projector is None:
        return raw
    return projector(event, raw)


def _source_event_id(raw: Mapping[str, Any]) -> str | None:
    return _first_string(
        raw.get("source_event_id"),
        raw.get("occurrence_id"),
        raw.get("event_id"),
        raw.get("eventId"),
        raw.get("tool_use_id"),
        raw.get("toolCallID"),
        raw.get("callID"),
    )


def _session(source: str, raw: Mapping[str, Any]) -> str | None:
    info = raw.get("sessionInfo")
    nested = info.get("id") if isinstance(info, Mapping) else None
    if source == "opencode":
        return _first_string(nested, raw.get("sessionId"), raw.get("sessionID"))
    return _string(raw.get("session_id"))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid integration event")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid integration event") from exc


def _tool_payload(source: str, raw: Mapping[str, Any]) -> dict[str, str]:
    tool_name = _first_string(raw.get("tool_name"), raw.get("tool")) or ""
    tool_name = {
        "edit": "Edit",
        "write": "Write",
        "multi_edit": "MultiEdit",
        "multiedit": "MultiEdit",
        "notebook_edit": "NotebookEdit",
        "notebookedit": "NotebookEdit",
        "bash": "Bash",
        "shell": "Bash",
    }.get(tool_name.lower(), tool_name)
    tool_input = raw.get("tool_input") if source != "opencode" else raw.get("input")
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
    return {"tool_name": tool_name, "target": target}


DELTA_SCALAR_NAMES = ("goal", "phase", "current_task")
DELTA_LIST_NAMES = (
    "next_actions",
    "decisions",
    "blockers",
    "changed_files",
    "commands",
    "verification",
)
DELTA_OPERATION_NAMES = tuple(f"{name}_operations" for name in DELTA_SCALAR_NAMES)
DELTA_LIST_LIMITS = {
    "next_actions": 10,
    "decisions": 100,
    "blockers": 100,
    "changed_files": 100,
    "commands": 100,
    "verification": 100,
}


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
    allowed = DELTA_SCALAR_NAMES + DELTA_LIST_NAMES + DELTA_OPERATION_NAMES + ("legacy_context",)
    if set(value) - set(allowed):
        raise ValueError("invalid project delta")


def _copy_scalar_deltas(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    for name in DELTA_SCALAR_NAMES:
        if name in value:
            canonical[name] = _canonical_delta_operation(value[name])


def _canonical_delta_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("invalid project delta")
    if len(value) > 10_000:
        raise ValueError("invalid project delta")
    return [_canonical_delta_operation(item) for item in value]


def _copy_delta_lists(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    for name in DELTA_OPERATION_NAMES + DELTA_LIST_NAMES:
        if name in value:
            canonical[name] = _canonical_delta_list(value[name])


def _copy_legacy_context(value: Mapping[str, Any], canonical: dict[str, object]) -> None:
    if "legacy_context" not in value:
        return
    legacy_context = value["legacy_context"]
    if not isinstance(legacy_context, str):
        raise ValueError("invalid project delta")
    if len(legacy_context) > 16384:
        raise ValueError("invalid project delta")
    canonical["legacy_context"] = legacy_context


def _canonical_project_delta(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid project delta")
    _validate_delta_names(value)
    canonical = _empty_delta()
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
    return {"prompt": _string(raw.get("prompt"))}


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
        return _tool_payload(source, raw)
    return builder(raw)


def _copy_checkpoint_signals(payload: dict[str, Any], raw: Mapping[str, Any]) -> None:
    for name in CHECKPOINT_SIGNAL_FIELDS:
        if name in raw:
            payload[name] = raw[name]


def _normalize_payload_delta(payload: dict[str, Any]) -> None:
    if "project_delta" in payload:
        payload["project_delta"] = _canonical_project_delta(payload["project_delta"])


def _copy_occurrence(payload: dict[str, Any], event: str, raw: Mapping[str, Any]) -> None:
    if event in OCCURRENCE_EVENTS and isinstance(raw.get("occurrence_id"), str):
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
    projected = _project_source_payload(source, event, raw)
    payload = _event_payload(source, event, projected)
    _copy_checkpoint_signals(payload, projected)
    _normalize_payload_delta(payload)
    _copy_occurrence(payload, event, projected)
    _apply_tool_mutation_defaults(payload, event)
    _apply_failure_default(payload, event)
    _apply_compaction_default(payload, event)

    source_time = occurred_at or _parse_timestamp(projected.get("timestamp"))
    return build_event_envelope(
        event_type=event,
        payload=payload,
        occurred_at=source_time,
        captured_at=captured_at,
        agent=source,
        session=_safe_string(_session(source, projected)),
        project=_safe_string(_string(projected.get("project"))),
        worktree=_safe_string(
            _first_string(
                projected.get("cwd"),
                projected.get("directory"),
                projected.get("projectRoot"),
            )
        ),
        severity=_safe_string(_string(projected.get("severity"))),
        parent_event_id=_safe_string(_string(projected.get("parent_event_id"))),
        source_event_id=_safe_string(_source_event_id(projected)),
        redact=redact_secrets,
    )


def _map_antigravity_stop(raw: Mapping[str, Any]) -> str:
    if _bounded_bool(raw.get("fullyIdle")):
        return "session_end"
    return "stop"


def _map_antigravity_event(event: str, raw: Mapping[str, Any]) -> str | None:
    if event == "session_start":
        if _bounded_index(raw.get("invocationNum")) != 0:
            return None
        return event
    if event == "stop":
        return _map_antigravity_stop(raw)
    return event


def normalize_host_event(
    source: str,
    event: str,
    raw: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> EventEnvelope | None:
    """Normalize one official host event, omitting protocol-only occurrences."""
    mapped_event = event
    if source == "antigravity":
        mapped_event = _map_antigravity_event(event, raw)
    if mapped_event is None:
        return None
    return normalize_event(
        source,
        mapped_event,
        raw,
        occurred_at=occurred_at,
        captured_at=captured_at,
    )


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
    if (
        event in OCCURRENCE_EVENTS
        and occurred_at is None
        and raw.get("timestamp") is None
        and _source_event_id(raw) is None
    ):
        normalized_raw = dict(raw)
        normalized_raw["occurrence_id"] = str(uuid.uuid4())
    return normalize_event(
        source,
        event,
        normalized_raw,
        occurred_at=occurred_at,
        captured_at=captured_at,
    )


def _run_delegate(
    name: str,
    payload: Mapping[str, Any],
    *,
    forward_stdout: bool = False,
    project_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if name not in DELEGATES:
        raise ValueError("invalid integration delegate")
    env = os.environ.copy()
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name)],
        cwd=str(ROOT),
        env=env,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=DELEGATE_TIMEOUT_SECONDS,
    )
    _forward_delegate_stdout(result, forward_stdout)
    return result


def _forward_delegate_stdout(
    result: subprocess.CompletedProcess[str], forward_stdout: bool
) -> None:
    if result.returncode != 0:
        return
    if not forward_stdout:
        return
    if _is_hook_output(result.stdout):
        sys.stdout.write(result.stdout)


def _is_hook_output(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    hook_output = parsed.get("hookSpecificOutput")
    return isinstance(hook_output, dict) and isinstance(hook_output.get("additionalContext"), str)


def _tool_capture_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": payload["tool_name"],
        "tool_input": {"filePath": payload["target"], "command": payload["target"]},
    }


def _prompt_capture_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"prompt": payload["prompt"]}


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


def _canonical_capture_payload(envelope: EventEnvelope) -> dict[str, Any]:
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


def _true_checkpoint_signal(payload: Mapping[str, Any]) -> str | None:
    names = (
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
    )
    return next((name for name in names if payload.get(name) is True), None)


def _explicit_observation_type(envelope: EventEnvelope) -> str:
    payload = envelope.payload
    if payload.get("compaction_confirmed") is True:
        return "compaction_confirmed"
    if isinstance(payload.get("token_percent"), (int, float)):
        return "token_usage"
    signal = _true_checkpoint_signal(payload)
    return signal or str(payload.get("checkpoint_type") or envelope.event_type)


def _is_plain_tool_observation(envelope: EventEnvelope, event_type: str) -> bool:
    return envelope.event_type == "post_tool_use" and event_type == "post_tool_use"


def _tool_observation_type(envelope: EventEnvelope, event_type: str) -> str:
    if not _is_plain_tool_observation(envelope, event_type):
        return event_type
    if envelope.severity in {"error", "fatal"}:
        return "significant_failure"
    if envelope.payload.get("changed") is not True:
        return event_type
    if envelope.payload.get("significant") is True:
        return "file_changed"
    return "mutation"


def _copy_observation_flags(observation: dict[str, object], payload: Mapping[str, Any]) -> None:
    for name in ("dirty", "changed", "significant"):
        if name in payload:
            observation[name] = payload[name]


def _checkpoint_observation(envelope: EventEnvelope) -> dict[str, object]:
    payload = envelope.payload
    event_type = _tool_observation_type(envelope, _explicit_observation_type(envelope))
    observation: dict[str, object] = {
        "type": event_type,
        "event_id": envelope.event_id,
    }
    _copy_observation_flags(observation, payload)
    if event_type == "token_usage":
        observation["percent"] = payload["token_percent"]
    return observation


def _empty_delta() -> dict[str, object]:
    close = {"id": "checkpoint-none", "action": "close", "value": ""}
    return {
        "goal": dict(close),
        "goal_operations": [],
        "phase": dict(close),
        "phase_operations": [],
        "current_task": dict(close),
        "current_task_operations": [],
        "next_actions": [],
        "decisions": [],
        "blockers": [],
        "changed_files": [],
        "commands": [],
        "verification": [],
        "legacy_context": "",
    }


def _checkpoint_event(
    envelope: EventEnvelope,
    slug: str,
    reason: str,
) -> dict[str, object]:
    delta = _checkpoint_delta(envelope)
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": envelope.event_id,
        "idempotency_key": f"{envelope.event_id}:{reason}",
        "provenance": {
            "agent": _known(envelope.agent),
            "session": _known(envelope.session),
            "worktree": _known(envelope.worktree),
            "branch": _known(_string(envelope.payload.get("branch"))),
            "source_event": _known(envelope.source_event_id, envelope.event_id),
        },
        "trigger": str(_checkpoint_observation(envelope)["type"]),
        "reason": reason,
        "delta": delta,
        "evidence_event_ids": [envelope.event_id],
    }


def _known(value: str | None, fallback: str = "unknown") -> str:
    if value:
        return value
    return fallback


def _checkpoint_delta(envelope: EventEnvelope) -> dict[str, object]:
    raw_delta = envelope.to_dict()["payload"].get("project_delta")
    if isinstance(raw_delta, Mapping):
        return dict(raw_delta)
    return _empty_delta()


def _pending_checkpoint(envelope: EventEnvelope, slug: str, state_key: str) -> dict[str, object]:
    return {
        "event_id": envelope.event_id,
        "state_key": state_key,
        "occurred_at": envelope.occurred_at.isoformat(),
        "observation": _checkpoint_observation(envelope),
        "checkpoint_event": _checkpoint_event(envelope, slug, "pending"),
        "has_project_delta": isinstance(envelope.payload.get("project_delta"), Mapping),
    }


def _delta_chunk_count(
    delta: Mapping[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
) -> int:
    scalar_counts = [(len(operations) + 99) // 100 for operations in scalar_operations.values()]
    list_counts = [
        (len(delta[name]) + limit - 1) // limit for name, limit in DELTA_LIST_LIMITS.items()
    ]
    return max([1] + scalar_counts + list_counts)


def _copy_chunk_context(chunk: dict[str, object], delta: Mapping[str, object], index: int) -> None:
    if index != 0:
        return
    context = delta.get("legacy_context")
    if isinstance(context, str):
        chunk["legacy_context"] = context


def _copy_scalar_chunk(
    chunk: dict[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
    index: int,
) -> None:
    for name, operations in scalar_operations.items():
        selected = operations[index * 100 : (index + 1) * 100]
        if selected:
            chunk[name] = selected[-1]
            chunk[f"{name}_operations"] = selected


def _copy_list_chunk(chunk: dict[str, object], delta: Mapping[str, object], index: int) -> None:
    for name, limit in DELTA_LIST_LIMITS.items():
        operations = delta[name]
        assert isinstance(operations, list)
        chunk[name] = operations[index * limit : (index + 1) * limit]


def _project_delta_chunk(
    delta: Mapping[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
    index: int,
) -> dict[str, object]:
    chunk = _empty_delta()
    _copy_chunk_context(chunk, delta, index)
    _copy_scalar_chunk(chunk, scalar_operations, index)
    _copy_list_chunk(chunk, delta, index)
    return chunk


def _split_project_delta(delta: Mapping[str, object]) -> list[dict[str, object]]:
    scalar_operations = {name: _scalar_delta_operations(delta, name) for name in DELTA_SCALAR_NAMES}
    chunk_count = _delta_chunk_count(delta, scalar_operations)
    if chunk_count == 1:
        return [dict(delta)]
    return [_project_delta_chunk(delta, scalar_operations, index) for index in range(chunk_count)]


def _pending_checkpoints(
    envelope: EventEnvelope, slug: str, state_key: str
) -> list[dict[str, object]]:
    pending = _pending_checkpoint(envelope, slug, state_key)
    delta = envelope.to_dict()["payload"].get("project_delta")
    if not isinstance(delta, Mapping):
        return [pending]
    chunks = _split_project_delta(delta)
    if len(chunks) == 1:
        return [pending]
    result: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        item = dict(pending)
        event_id = f"{envelope.event_id}:part:{index + 1}"
        item["event_id"] = event_id
        item["has_project_delta"] = True
        checkpoint = dict(pending["checkpoint_event"])
        checkpoint["occurrence_id"] = event_id
        checkpoint["idempotency_key"] = f"{event_id}:pending"
        checkpoint["delta"] = chunk
        checkpoint["evidence_event_ids"] = [event_id]
        item["checkpoint_event"] = checkpoint
        if index < len(chunks) - 1:
            item["observation"] = {
                "type": "coalesced_delta",
                "event_id": event_id,
            }
        else:
            observation = dict(pending["observation"])
            observation["event_id"] = event_id
            item["observation"] = observation
        result.append(item)
    return result


def _release_claims(state: dict[str, Any], queue_key: str, owner: str) -> None:
    pending = state.get("project_checkpoint_pending")
    if not isinstance(pending, dict):
        return
    queue = pending.get(queue_key)
    if not isinstance(queue, list):
        return
    for item in queue:
        if item.get("claim_owner") == owner:
            item.pop("claim_owner", None)
            item.pop("claim_until", None)


def _release_pending_claims(queue_key: str, owner: str) -> None:
    def release(state: dict[str, Any]) -> None:
        _release_claims(state, queue_key, owner)

    update_state(release, lock_timeout=0.5)


def _has_pending_delta(item: Mapping[str, object]) -> bool:
    if "has_project_delta" in item:
        return item.get("has_project_delta") is True
    checkpoint = item.get("checkpoint_event")
    if not isinstance(checkpoint, Mapping):
        return False
    delta = checkpoint.get("delta")
    if not isinstance(delta, Mapping):
        return False
    normalized = dict(delta)
    normalized.setdefault("current_task_operations", [])
    return normalized != _empty_delta()


def _scalar_delta_operations(delta: Mapping[str, object], name: str) -> list[dict[str, object]]:
    supplied = delta.get(f"{name}_operations")
    if isinstance(supplied, list):
        if supplied:
            return _mapping_dicts(supplied)
    return _single_delta_operation(delta[name])


def _mapping_dicts(values: Sequence[object]) -> list[dict[str, object]]:
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _single_delta_operation(operation: object) -> list[dict[str, object]]:
    assert isinstance(operation, Mapping)
    if _is_empty_delta_operation(operation):
        return []
    return [dict(operation)]


def _is_empty_delta_operation(operation: Mapping[str, object]) -> bool:
    return operation.get("id") == "checkpoint-none" and operation.get("action") == "close"


def _pending_delta(item: Mapping[str, object]) -> Mapping[str, object] | None:
    if not _has_pending_delta(item):
        return None
    checkpoint = item["checkpoint_event"]
    assert isinstance(checkpoint, Mapping)
    delta = checkpoint["delta"]
    assert isinstance(delta, Mapping)
    return delta


def _operation_ids(values: Sequence[object]) -> set[str]:
    return {str(operation["id"]) for operation in values if isinstance(operation, Mapping)}


def _accumulate_batch_delta(
    delta: Mapping[str, object],
    scalar_counts: dict[str, int],
    list_ids: dict[str, set[str]],
) -> None:
    for name in scalar_counts:
        scalar_counts[name] += len(_scalar_delta_operations(delta, name))
    for name, ids in list_ids.items():
        operations = delta[name]
        assert isinstance(operations, list)
        ids.update(_operation_ids(operations))


def _batch_limits_exceeded(
    evidence: set[str],
    scalar_counts: Mapping[str, int],
    list_ids: Mapping[str, set[str]],
) -> bool:
    if len(evidence) > 100:
        return True
    if any(count > 100 for count in scalar_counts.values()):
        return True
    return any(len(ids) > DELTA_LIST_LIMITS[name] for name, ids in list_ids.items())


def _bounded_pending_batch_count(items: Sequence[Mapping[str, object]]) -> int:
    evidence: set[str] = set()
    scalar_counts = {name: 0 for name in DELTA_SCALAR_NAMES}
    list_ids = {name: set() for name in DELTA_LIST_NAMES}
    accepted = 0
    for item in items:
        next_evidence, next_scalar_counts, next_list_ids = _next_batch_state(
            item, evidence, scalar_counts, list_ids
        )
        if _batch_limits_exceeded(next_evidence, next_scalar_counts, next_list_ids):
            break
        evidence, scalar_counts, list_ids = next_evidence, next_scalar_counts, next_list_ids
        accepted += 1
    return max(1, accepted)


def _next_batch_state(
    item: Mapping[str, object],
    evidence: set[str],
    scalar_counts: Mapping[str, int],
    list_ids: Mapping[str, set[str]],
) -> tuple[set[str], dict[str, int], dict[str, set[str]]]:
    next_evidence = evidence | {str(item["event_id"])}
    next_scalar_counts = dict(scalar_counts)
    next_list_ids = {name: set(values) for name, values in list_ids.items()}
    delta = _pending_delta(item)
    if delta is not None:
        _accumulate_batch_delta(delta, next_scalar_counts, next_list_ids)
    return next_evidence, next_scalar_counts, next_list_ids


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _merge_context(delta: Mapping[str, object], contexts: list[str]) -> None:
    context = delta.get("legacy_context")
    if not isinstance(context, str):
        return
    if context:
        _append_unique(contexts, context)


def _merge_scalar_operations(
    delta: Mapping[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
) -> None:
    for name, operations in scalar_operations.items():
        operations.extend(_scalar_delta_operations(delta, name))


def _merge_list_operations(
    delta: Mapping[str, object],
    list_operations: Mapping[str, dict[str, dict[str, object]]],
) -> None:
    for name, operations in list_operations.items():
        values = delta[name]
        assert isinstance(values, list)
        for operation in values:
            assert isinstance(operation, Mapping)
            item_id = str(operation["id"])
            operations.pop(item_id, None)
            operations[item_id] = dict(operation)


def _merge_pending_item(
    item: Mapping[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
    list_operations: Mapping[str, dict[str, dict[str, object]]],
    evidence: list[str],
    contexts: list[str],
) -> None:
    _append_unique(evidence, str(item["event_id"]))
    delta = _pending_delta(item)
    if delta is None:
        return
    _merge_context(delta, contexts)
    _merge_scalar_operations(delta, scalar_operations)
    _merge_list_operations(delta, list_operations)


def _copy_merged_scalars(
    merged: dict[str, object],
    scalar_operations: Mapping[str, list[dict[str, object]]],
) -> None:
    for name, operations in scalar_operations.items():
        if operations:
            merged[name] = operations[-1]
            merged[f"{name}_operations"] = operations


def _build_merged_delta(
    scalar_operations: Mapping[str, list[dict[str, object]]],
    list_operations: Mapping[str, dict[str, dict[str, object]]],
    contexts: Sequence[str],
) -> dict[str, object]:
    merged = _empty_delta()
    _copy_merged_scalars(merged, scalar_operations)
    for name, operations in list_operations.items():
        merged[name] = list(operations.values())
    if contexts:
        merged["legacy_context"] = "\n\n".join(contexts)[:16384]
    return merged


def _merge_pending_checkpoints(
    items: Sequence[Mapping[str, object]], decision: CheckpointDecision
) -> dict[str, object]:
    scalar_operations = {name: [] for name in DELTA_SCALAR_NAMES}
    list_operations = {name: {} for name in DELTA_LIST_NAMES}
    evidence: list[str] = []
    contexts: list[str] = []
    for item in items:
        _merge_pending_item(item, scalar_operations, list_operations, evidence, contexts)
    checkpoint = dict(items[-1]["checkpoint_event"])
    event_id = str(items[-1]["event_id"])
    checkpoint.update(
        {
            "occurrence_id": event_id,
            "idempotency_key": f"{event_id}:{decision.reason}",
            "reason": decision.reason,
            "delta": _build_merged_delta(scalar_operations, list_operations, contexts),
            "evidence_event_ids": evidence,
        }
    )
    return checkpoint


def _pending_queue(state: Mapping[str, Any], queue_key: str) -> list[Any] | None:
    pending = state.get("project_checkpoint_pending")
    if not isinstance(pending, dict):
        return None
    queue = pending.get(queue_key)
    if not isinstance(queue, list):
        return None
    return queue


def _claim_available(item: Mapping[str, Any], owner: str, now: float) -> bool:
    if item.get("claim_owner") in {None, owner}:
        return True
    claim_until = item.get("claim_until")
    if not isinstance(claim_until, (int, float)):
        return True
    return claim_until <= now


def _claim_queue(queue: Sequence[dict[str, Any]], owner: str, now: float) -> bool:
    if not all(_claim_available(item, owner, now) for item in queue):
        return False
    for item in queue:
        item["claim_owner"] = owner
        item["claim_until"] = now + PENDING_CLAIM_SECONDS
    return True


def _copy_reducer_states(state: Mapping[str, Any]) -> dict[str, object]:
    reducers = state.get("project_checkpoint_reducers")
    if isinstance(reducers, dict):
        return dict(reducers)
    return {}


def _claim_pending_state(
    state: dict[str, Any],
    queue_key: str,
    owner: str,
    claimed: list[tuple[list[dict[str, object]], dict[str, object]]],
) -> None:
    queue = _pending_queue(state, queue_key)
    if not queue:
        return
    if not _claim_queue(queue, owner, time.time()):
        return
    claimed.append(([dict(item) for item in queue], _copy_reducer_states(state)))


def _claim_pending(
    queue_key: str, owner: str
) -> tuple[list[dict[str, object]], dict[str, object]] | None:
    claimed: list[tuple[list[dict[str, object]], dict[str, object]]] = []

    def claim(state: dict[str, Any]) -> None:
        _claim_pending_state(state, queue_key, owner, claimed)

    update_state(claim, lock_timeout=0.5)
    if claimed:
        return claimed[0]
    return None


def _reducer_for(
    reducers: dict[str, CheckpointReducer],
    reducer_states: Mapping[str, object],
    state_key: str,
) -> CheckpointReducer:
    if state_key not in reducers:
        reducer_state = reducer_states.get(state_key)
        initial = reducer_state if isinstance(reducer_state, Mapping) else None
        reducers[state_key] = CheckpointReducer.from_state(initial)
    return reducers[state_key]


def _observe_pending_item(
    item: Mapping[str, object],
    reducers: dict[str, CheckpointReducer],
    reducer_states: Mapping[str, object],
) -> CheckpointDecision | None:
    state_key = str(item["state_key"])
    observation = item["observation"]
    assert isinstance(observation, Mapping)
    occurred_at = datetime.fromisoformat(str(item["occurred_at"]))
    reducer = _reducer_for(reducers, reducer_states, state_key)
    return reducer.observe(observation, now=occurred_at, commit=False)


def _is_checkpoint_decision(decision: CheckpointDecision | None) -> bool:
    return decision is not None and not decision.maintenance


def _observe_until_checkpoint(
    items: Sequence[Mapping[str, object]], reducer_states: Mapping[str, object]
) -> tuple[
    dict[str, CheckpointReducer],
    list[CheckpointDecision | None],
    int | None,
    CheckpointDecision | None,
]:
    reducers: dict[str, CheckpointReducer] = {}
    decisions: list[CheckpointDecision | None] = []
    for index, item in enumerate(items):
        decision = _observe_pending_item(item, reducers, reducer_states)
        decisions.append(decision)
        if _is_checkpoint_decision(decision):
            return reducers, decisions, index, decision
    return reducers, decisions, None, None


def _delta_due(
    item: Mapping[str, object],
    reducers: Mapping[str, CheckpointReducer],
    latest: datetime,
) -> bool:
    if not _has_pending_delta(item):
        return False
    previous = reducers[str(item["state_key"])].last_checkpoint_at
    if previous is None:
        return True
    return latest - previous >= timedelta(seconds=30)


def _resolve_debounce(
    items: Sequence[Mapping[str, object]],
    reducers: Mapping[str, CheckpointReducer],
    index: int | None,
    decision: CheckpointDecision | None,
) -> tuple[int | None, CheckpointDecision | None, bool]:
    if index is not None:
        return index, decision, False
    if not _any_pending_delta(items):
        return None, None, False
    latest = datetime.fromisoformat(str(items[-1]["occurred_at"]))
    if _any_delta_due(items, reducers, latest):
        return len(items) - 1, CheckpointDecision("debounce_flush", checkpoint_at=latest), False
    return None, None, True


def _any_pending_delta(items: Sequence[Mapping[str, object]]) -> bool:
    return any(_has_pending_delta(item) for item in items)


def _any_delta_due(
    items: Sequence[Mapping[str, object]],
    reducers: Mapping[str, CheckpointReducer],
    latest: datetime,
) -> bool:
    return any(_delta_due(item, reducers, latest) for item in items)


def _observe_all(
    items: Sequence[Mapping[str, object]], reducer_states: Mapping[str, object]
) -> tuple[dict[str, CheckpointReducer], list[CheckpointDecision | None]]:
    reducers: dict[str, CheckpointReducer] = {}
    decisions = [_observe_pending_item(item, reducers, reducer_states) for item in items]
    return reducers, decisions


def _target_count(index: int | None, item_count: int) -> int:
    if index is None:
        return item_count
    return index + 1


def _batch_plan(
    items: Sequence[Mapping[str, object]],
    reducer_states: Mapping[str, object],
    reducers: dict[str, CheckpointReducer],
    decisions: list[CheckpointDecision | None],
    checkpoint_index: int | None,
    checkpoint_decision: CheckpointDecision | None,
) -> tuple[
    list[dict[str, object]],
    dict[str, CheckpointReducer],
    list[CheckpointDecision | None],
    CheckpointDecision | None,
]:
    target = _target_count(checkpoint_index, len(items))
    if checkpoint_decision is None:
        return list(items[:target]), reducers, decisions[:target], None
    flush_count = min(target, _bounded_pending_batch_count(items[:target]))
    if flush_count == target:
        selected_decisions = decisions[:flush_count]
        selected_decisions.extend([None] * (flush_count - len(selected_decisions)))
        return list(items[:flush_count]), reducers, selected_decisions, checkpoint_decision
    selected = list(items[:flush_count])
    selected_time = datetime.fromisoformat(str(selected[-1]["occurred_at"]))
    batch_decision = CheckpointDecision("batch_flush", checkpoint_at=selected_time)
    reducers, decisions = _observe_all(selected, reducer_states)
    return selected, reducers, decisions, batch_decision


def _write_project_checkpoint(
    slug: str,
    selected: Sequence[Mapping[str, object]],
    decision: CheckpointDecision,
    writer_wait_seconds: float | None,
) -> None:
    checkpoint = _merge_pending_checkpoints(selected, decision)
    event_id = str(selected[-1]["event_id"])
    args = (slug, checkpoint, f"lifecycle:{event_id[:16]}")
    store = ProjectStore(ROOT, STATE_ROOT)
    if writer_wait_seconds is None:
        store.checkpoint(*args)
        return
    store.checkpoint(*args, writer_wait_seconds=writer_wait_seconds)


def _commit_maintenance_observations(
    selected: Sequence[Mapping[str, object]],
    decisions: Sequence[CheckpointDecision | None],
    reducers: Mapping[str, CheckpointReducer],
) -> None:
    for item, decision in zip(selected, decisions):
        if decision is not None:
            reducers[str(item["state_key"])].commit_observation(decision, outcome="maintenance")


def _persist_selected(
    slug: str,
    selected: Sequence[Mapping[str, object]],
    decisions: Sequence[CheckpointDecision | None],
    reducers: Mapping[str, CheckpointReducer],
    checkpoint_decision: CheckpointDecision | None,
    writer_wait_seconds: float | None,
) -> dict[str, object]:
    if checkpoint_decision is None:
        _commit_maintenance_observations(selected, decisions, reducers)
    else:
        _write_project_checkpoint(slug, selected, checkpoint_decision, writer_wait_seconds)
        reducers[str(selected[-1]["state_key"])].commit_observation(
            checkpoint_decision, outcome="checkpoint"
        )
    return {state_key: reducer.to_state() for state_key, reducer in reducers.items()}


def _validate_pending_commit(
    queue: Sequence[Mapping[str, object]],
    selected: Sequence[Mapping[str, object]],
    owner: str,
) -> None:
    expected_ids = [str(item["event_id"]) for item in selected]
    actual_ids = [str(item.get("event_id")) for item in queue[: len(selected)]]
    if actual_ids != expected_ids:
        raise RuntimeError("project checkpoint pending prefix changed")
    if not _claims_match(queue[: len(selected)], owner):
        raise RuntimeError("project checkpoint pending claim changed")


def _claims_match(queue: Sequence[Mapping[str, object]], owner: str) -> bool:
    return all(item.get("claim_owner") == owner for item in queue)


def _trim_reducers(reducers: dict[str, object]) -> None:
    if len(reducers) > 128:
        reducers.pop(next(iter(reducers)))


def _commit_pending_state(
    state: dict[str, Any],
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    committed_reducers: Mapping[str, object],
) -> None:
    pending = state.setdefault("project_checkpoint_pending", {})
    queue = pending.setdefault(queue_key, [])
    if not queue:
        return
    _validate_pending_commit(queue, selected, owner)
    reducers = state.setdefault("project_checkpoint_reducers", {})
    reducers.update(committed_reducers)
    del queue[: len(selected)]
    _release_claims(state, queue_key, owner)
    _trim_reducers(reducers)


def _commit_pending(
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    committed_reducers: Mapping[str, object],
) -> None:
    def commit(state: dict[str, Any]) -> None:
        _commit_pending_state(state, queue_key, owner, selected, committed_reducers)

    update_state(commit, lock_timeout=0.5)


def _persist_or_release(
    slug: str,
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    decisions: Sequence[CheckpointDecision | None],
    reducers: Mapping[str, CheckpointReducer],
    checkpoint_decision: CheckpointDecision | None,
    writer_wait_seconds: float | None,
) -> dict[str, object]:
    try:
        return _persist_selected(
            slug,
            selected,
            decisions,
            reducers,
            checkpoint_decision,
            writer_wait_seconds,
        )
    except Exception:
        _release_pending_claims(queue_key, owner)
        raise


def _commit_or_release(
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    committed_reducers: Mapping[str, object],
) -> None:
    try:
        _commit_pending(queue_key, owner, selected, committed_reducers)
    except Exception:
        _release_pending_claims(queue_key, owner)
        raise


def _drain_project_checkpoint_once(
    slug: str,
    queue_key: str,
    owner: str,
    writer_wait_seconds: float | None,
) -> bool:
    claimed = _claim_pending(queue_key, owner)
    if claimed is None:
        return False
    items, reducer_states = claimed
    reducers, decisions, index, decision = _observe_until_checkpoint(items, reducer_states)
    index, decision, waiting = _resolve_debounce(items, reducers, index, decision)
    if waiting:
        _release_pending_claims(queue_key, owner)
        return False
    selected, reducers, decisions, decision = _batch_plan(
        items, reducer_states, reducers, decisions, index, decision
    )
    committed = _persist_or_release(
        slug,
        queue_key,
        owner,
        selected,
        decisions,
        reducers,
        decision,
        writer_wait_seconds,
    )
    _commit_or_release(queue_key, owner, selected, committed)
    return True


def _drain_project_checkpoints(
    slug: str,
    queue_key: str,
    *,
    writer_wait_seconds: float | None = None,
) -> None:
    owner = f"{os.getpid()}:{secrets.token_hex(8)}"
    while _drain_project_checkpoint_once(slug, queue_key, owner, writer_wait_seconds):
        pass


def _observe_project_checkpoint(
    envelope: EventEnvelope,
    *,
    writer_wait_seconds: float | None = None,
) -> None:
    """Durably enqueue one envelope and drain its project's ordered queue."""
    slug, project_dir = _project_context(envelope)
    if not slug or project_dir is None:
        return
    session_key = envelope.session or "unknown"
    state_key = f"{slug}:{session_key}"
    pending_events = _pending_checkpoints(envelope, slug, state_key)

    def enqueue(state: dict[str, Any]) -> None:
        _enqueue_pending_events(state, state_key, slug, pending_events)

    update_state(enqueue, lock_timeout=0.5)
    _drain_project_checkpoints(slug, slug, writer_wait_seconds=writer_wait_seconds)


def _observed_event_ids(state: Mapping[str, Any], state_key: str) -> set[object]:
    reducers = state.get("project_checkpoint_reducers")
    if not isinstance(reducers, dict):
        return set()
    reducer_state = reducers.get(state_key)
    if not isinstance(reducer_state, Mapping):
        return set()
    return set(reducer_state.get("observed_event_ids", []))


def _enqueue_pending_events(
    state: dict[str, Any],
    state_key: str,
    slug: str,
    pending_events: Sequence[dict[str, object]],
) -> None:
    observed = _observed_event_ids(state, state_key)
    pending = state.setdefault("project_checkpoint_pending", {})
    queue = pending.setdefault(slug, [])
    queued = {item.get("event_id") for item in queue}
    for pending_event in pending_events:
        event_id = pending_event["event_id"]
        if event_id not in observed and event_id not in queued:
            queue.append(pending_event)
            queued.add(event_id)


def _bounded_checkpoint_error(error: BaseException) -> str:
    message = redact_secrets(f"{type(error).__name__}: {error}")
    return " ".join(message.split())[:MAX_CHECKPOINT_ERROR_CHARS]


def _log_checkpoint_error(error: BaseException) -> None:
    """Best-effort bounded diagnostics for fail-open lifecycle capture."""
    try:
        message = _bounded_checkpoint_error(error)
        log_path = STATE_ROOT / "logs" / "hook-errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] project checkpoint: {message}\n")
    except Exception:  # noqa: BLE001
        pass


def _observe_checkpoint_fail_open(envelope: EventEnvelope) -> None:
    try:
        if envelope.event_type == "session_start":
            _observe_project_checkpoint(
                envelope, writer_wait_seconds=SESSION_START_RECOVERY_SECONDS
            )
        else:
            _observe_project_checkpoint(envelope)
    except Exception as exc:  # noqa: BLE001
        _log_checkpoint_error(exc)


def _project_context(envelope: EventEnvelope) -> tuple[str | None, Path | None]:
    if not envelope.worktree:
        return None, None
    try:
        project_dir = Path(envelope.worktree).resolve()
        slug = _compute_slug(project_dir, ROOT / "knowledge" / "projects")
    except (OSError, ValueError):
        return None, None
    return slug, project_dir


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _validate_transient_dir(
    state_root: Path,
    path: Path,
    expected: os.stat_result | None = None,
) -> os.stat_result:
    info = path.lstat()
    _validate_transient_kind(path, info)
    _validate_transient_containment(state_root, path)
    if expected is None:
        return info
    if not _same_identity(info, expected):
        raise PermissionError("transient directory identity changed")
    return info


def _validate_transient_kind(path: Path, info: os.stat_result) -> None:
    if _is_reparse_point(path):
        raise PermissionError("transient directory is not secure")
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("transient directory is not secure")


def _validate_transient_containment(state_root: Path, path: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("transient directory escaped state root") from exc


def _create_transient_parent(state_root: Path) -> tuple[Path, os.stat_result]:
    current = state_root
    for name in ("cache", "transient-transcripts"):
        current = current / name
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = _validate_transient_dir(state_root, current)
    return current, info


def _secure_posix_parent(state_root: Path, current: Path, info: os.stat_result) -> os.stat_result:
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise PermissionError("transient directory is not private")
    if mode != 0o700:
        current.chmod(0o700)
    secured = _validate_transient_dir(state_root, current)
    if stat.S_IMODE(secured.st_mode) != 0o700:
        raise PermissionError("transient directory is not private")
    return secured


def _secure_windows_parent(state_root: Path, current: Path, info: os.stat_result) -> os.stat_result:
    _restrict_file_permissions(current)
    return _validate_transient_dir(state_root, current, info)


def _secure_transient_dir() -> tuple[Path, Path, os.stat_result]:
    state_root = Path(STATE_ROOT).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    current, info = _create_transient_parent(state_root)
    if os.name == "posix":
        info = _secure_posix_parent(state_root, current, info)
    else:
        info = _secure_windows_parent(state_root, current, info)
    return state_root, current, info


def _same_file(path: Path, opened: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _cleanup_created_transient(path: Path, opened: os.stat_result) -> None:
    if _same_file(path, opened):
        try:
            path.unlink()
        except OSError:
            pass


def _write_all(descriptor: int, text: str) -> None:
    remaining = memoryview(text.encode("utf-8"))
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("transient write failed")
        remaining = remaining[written:]


def _same_file_at(directory_fd: int, name: str, opened: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and _same_identity(current, opened)


def _validate_posix_parent_fd(directory_fd: int, parent_info: os.stat_result) -> os.stat_result:
    directory_info = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_info.st_mode):
        raise PermissionError("transient directory identity changed")
    if not _same_identity(directory_info, parent_info):
        raise PermissionError("transient directory identity changed")
    return directory_info


def _open_posix_transient(directory_fd: int, event_id: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        name = f"{event_id}-{secrets.token_hex(16)}.txt"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("could not create unique transient transcript")


def _write_open_posix_transient(
    descriptor: int,
    directory_fd: int,
    name: str,
    text: str,
    state_root: Path,
    parent: Path,
    directory_info: os.stat_result,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError("transient file is not regular")
    os.fchmod(descriptor, 0o600)
    _write_all(descriptor, text)
    os.fsync(descriptor)
    _validate_transient_dir(state_root, parent, directory_info)
    if not _same_file_at(directory_fd, name, opened):
        raise PermissionError("transient file identity changed")
    return opened


def _unlink_posix_transient(
    directory_fd: int, name: str | None, opened: os.stat_result | None
) -> None:
    if name is None or opened is None:
        return
    if not _same_file_at(directory_fd, name, opened):
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def _close_descriptor(descriptor: int) -> None:
    if descriptor >= 0:
        os.close(descriptor)


def _write_posix_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = -1
    name: str | None = None
    opened: os.stat_result | None = None
    succeeded = False
    try:
        directory_info = _validate_posix_parent_fd(directory_fd, parent_info)
        descriptor, name = _open_posix_transient(directory_fd, envelope.event_id)
        # Capture the identity at creation time. Taking it from the write result
        # instead would leave `opened` unset whenever the write or the
        # post-write parent validation fails, and the cleanup below would then
        # skip an already-created transcript containing session content.
        opened = os.fstat(descriptor)
        _write_open_posix_transient(
            descriptor, directory_fd, name, text, state_root, parent, directory_info
        )
        succeeded = True
        return parent / name
    finally:
        if not succeeded:
            _unlink_posix_transient(directory_fd, name, opened)
        _close_descriptor(descriptor)
        os.close(directory_fd)


def _raw_windows_path_from_fd(descriptor: int) -> str | None:
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    get_final_path.restype = ctypes.c_uint32
    length = get_final_path(handle, None, 0, 0)
    if not length:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    if not get_final_path(handle, buffer, len(buffer), 0):
        return None
    return buffer.value


def _normalize_windows_path(value: str) -> Path:
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


def _windows_path_from_fd(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
        value = _raw_windows_path_from_fd(descriptor)
    except (ImportError, OSError, ValueError):
        return None
    if value is None:
        return None
    return _normalize_windows_path(value)


def _open_windows_transient(parent: Path, event_id: str) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        path = parent / f"{event_id}-{secrets.token_hex(16)}.txt"
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError("could not create unique transient transcript")


def _validate_windows_opened(
    path: Path, opened: os.stat_result, state_root: Path, parent: Path
) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError("transient file is not regular")
    resolved = path.resolve(strict=True)
    resolved.relative_to(state_root)
    if resolved.parent != parent.resolve(strict=True):
        raise PermissionError("transient file containment changed")
    if not _same_file(path, opened):
        raise PermissionError("transient file containment changed")


def _write_open_windows_transient(
    descriptor: int,
    path: Path,
    opened: os.stat_result,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    _validate_windows_opened(path, opened, state_root, parent)
    _restrict_file_permissions(path)
    if not _same_file(path, opened):
        raise PermissionError("transient file identity changed")
    _write_all(descriptor, text)
    os.fsync(descriptor)
    cleanup_path = _windows_path_from_fd(descriptor)
    _validate_transient_dir(state_root, parent, parent_info)
    if not _same_file(path, opened):
        raise PermissionError("transient file identity changed")
    return cleanup_path or path


def _write_windows_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    descriptor, path = _open_windows_transient(parent, envelope.event_id)
    opened: os.stat_result | None = None
    cleanup_path = path
    try:
        opened = os.fstat(descriptor)
        cleanup_path = _write_open_windows_transient(
            descriptor, path, opened, text, state_root, parent, parent_info
        )
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        os.close(descriptor)
        if opened is not None:
            _cleanup_created_transient(cleanup_path, opened)
        raise
    os.close(descriptor)
    return path


def _write_transient_transcript(envelope: EventEnvelope, text: str) -> Path:
    state_root, parent, parent_info = _secure_transient_dir()
    if os.name == "posix":
        return _write_posix_transient(envelope, text, state_root, parent, parent_info)
    return _write_windows_transient(envelope, text, state_root, parent, parent_info)


def _restrict_file_permissions(path: Path) -> None:
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        if not username:
            raise PermissionError("transient permissions unavailable")
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:(R,W)",
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            raise PermissionError("transient permissions unavailable")
        return
    path.chmod(0o600)


def _record_activity(
    envelope: EventEnvelope,
    slug: str | None,
    project_dir: Path | None,
) -> bool:
    if not slug or project_dir is None:
        return False
    heartbeat = _run_delegate(
        "heartbeat_record.py",
        {
            "slug": slug,
            "projectRoot": str(project_dir),
            "reason": envelope.payload.get("reason") or envelope.event_type,
            "sessionId": envelope.session,
        },
        project_dir=project_dir,
    )
    return getattr(heartbeat, "returncode", 0) == 0


def _flush_started(result: subprocess.CompletedProcess[str]) -> bool:
    if getattr(result, "returncode", 1) != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("flush_started") is True


def _cleanup_runtime_transient(path: Path) -> None:
    try:
        candidate = path.resolve()
        candidate.relative_to((STATE_ROOT / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _run_session_start_maintenance() -> int:
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "work"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=MAINTENANCE_DRAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        spawn_compile_if_idle()
    except Exception:  # noqa: BLE001
        pass
    return 0


def _recover_project_handoff(slug: str | None, project_dir: Path | None) -> Sequence[Any]:
    if not slug or project_dir is None:
        return ()
    try:
        store = ProjectStore(ROOT, STATE_ROOT)
        return recover_project_handoff(
            store,
            slug,
            project_root=project_dir,
            render_context=False,
        ).items
    except Exception as exc:  # noqa: BLE001
        _log_checkpoint_error(exc)
        return ()


def _global_context_items(value: Sequence[Any] | str, item_type: type) -> list[Any]:
    if not isinstance(value, str):
        return list(value)
    text = value.strip()
    if not text:
        return []
    return [
        item_type(
            item_id="session-start:unstructured",
            text=text,
            source="session-start",
            priority=5,
            relevance=0.5,
            confidence="medium",
            freshness="fresh",
            token_cost=len(text.encode("utf-8")),
            mandatory=False,
            representation="l1",
            parent_id="session-start",
            priority_class="evidence",
        )
    ]


def _append_handoff_item(items: list[Any], handoff: Sequence[Any] | str, item_type: type) -> None:
    if not isinstance(handoff, str):
        items.extend(handoff)
        return
    text = handoff.strip()
    if not text:
        return
    items.append(
        item_type(
            item_id="session-start:project-handoff",
            text=text,
            source="project-handoff",
            priority=3,
            relevance=1.0,
            confidence="high",
            freshness="fresh",
            token_cost=len(text.encode("utf-8")),
            mandatory=True,
            representation="l1",
            parent_id="project-handoff",
            priority_class="handoff",
        )
    )


def _compile_context(items: Sequence[Any]) -> str:
    from context_budget import DEFAULT_CONTEXT_BUDGET, BudgetExceededError
    from context_compiler import compile_context_items

    try:
        return compile_context_items(
            items,
            budget=DEFAULT_CONTEXT_BUDGET,
            emergency_byte_cap=DEFAULT_CONTEXT_BUDGET.available_input_tokens,
        ).text
    except BudgetExceededError as error:
        return error.failure.render()


def _append_context(
    context_items: Sequence[Any] | str,
    handoff: Sequence[Any] | str,
    *,
    trailing_newline: bool = False,
) -> str:
    from context_budget import ContextItem

    items = _global_context_items(context_items, ContextItem)
    _append_handoff_item(items, handoff, ContextItem)
    if not items:
        return ""
    rendered = _compile_context(items)
    if trailing_newline:
        return rendered + "\n"
    return rendered


def _ingest_result(slug: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "slug": slug,
        "heartbeat_recorded": False,
        "daily_log_written": False,
        "flush_spawned": False,
        "transcript_path": payload.get("transcript_path"),
        "returncode": 0,
    }


def _ingest_session_start(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    result["heartbeat_recorded"] = _record_activity(envelope, slug, project_dir)
    maintenance_pid = spawn_detached(
        [sys.executable, str(SCRIPTS_DIR / "integration_adapter.py"), "--maintenance"]
    )
    result["maintenance_scheduled"] = maintenance_pid is not None
    result["context"] = _append_context(
        build_session_start_context(),
        _recover_project_handoff(slug, project_dir),
        trailing_newline=True,
    )


def _ingest_user_prompt(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    _run_delegate(
        "user_prompt_capture.py",
        payload,
        forward_stdout=envelope.agent not in IDE_SOURCES,
        project_dir=project_dir,
    )
    _run_delegate(
        "feedback_capture.py",
        {
            "text": payload["prompt"],
            "session_id": envelope.session or "unknown",
            "slug": slug or "unknown",
            "trigger": f"{envelope.agent or 'unknown'}-user-message",
        },
        project_dir=project_dir,
    )


def _ingest_post_tool(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    _run_delegate("post_tool_capture.py", payload, project_dir=project_dir)


def _capture_path_is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _capture_path_text_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capture transcript path is unavailable")
    if not value:
        raise ValueError("capture transcript path is unavailable")
    return value


def _validated_capture_transcript_path(value: object) -> Path:
    text = _capture_path_text_value(value)
    path = Path(text).resolve(strict=True)
    if path.suffix.casefold() not in {".jsonl", ".json", ".txt", ".log"}:
        raise PermissionError("capture transcript extension is not allowed")
    roots = (
        Path.home() / ".claude" / "projects",
        Path.home() / ".codex" / "sessions",
        Path(STATE_ROOT) / "cache" / "transient-transcripts",
    )
    if not any(_capture_path_is_beneath(path, root) for root in roots):
        raise PermissionError("capture transcript path is not allowed")
    return path


def _capture_path_evidence(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value:
        return None
    from bounded_io import read_stable_utf8

    path = _validated_capture_transcript_path(value)
    text = read_stable_utf8(
        path,
        MAX_CAPTURE_EVIDENCE_BYTES,
        label="capture transcript",
    )
    redacted = redact_secrets(text)
    if not redacted:
        return None
    return redacted


def _capture_evidence_text(envelope: EventEnvelope, payload: Mapping[str, Any]) -> str | None:
    inline = envelope.payload.get("transcript_text")
    if isinstance(inline, str):
        if not inline:
            return None
        return inline
    return _capture_path_evidence(payload.get("transcript_path"))


def _capture_nullable_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} is invalid")
    return value


def _capture_source_record(
    envelope: EventEnvelope,
    slug: str | None,
    trigger: str | None,
    text: str,
) -> dict[str, object]:
    evidence = [{"role": "transcript", "parts": [{"type": "text", "text": text}]}]
    return {
        "source_occurrence_id": envelope.event_id,
        "source_event_id": envelope.source_event_id or envelope.event_id,
        "occurred_at": None,
        "host": envelope.agent or "unknown",
        "event": envelope.event_type,
        "session": _capture_nullable_text(envelope.session, "capture session"),
        "project_slug": _capture_nullable_text(slug, "capture project slug"),
        "worktree": _capture_nullable_text(envelope.worktree, "capture worktree"),
        "trigger": _capture_nullable_text(trigger, "capture trigger"),
        "checkpoint_reason": _capture_nullable_text(
            envelope.payload.get("reason"), "capture checkpoint reason"
        ),
        "chunk_index": 0,
        "chunk_count": 1,
        "evidence": evidence,
    }


def _capture_intent_record(source: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

    evidence = source["evidence"]
    complete_digest = sha256_bytes(canonical_json_bytes(dict(source)))
    chunk_digest = sha256_bytes(canonical_json_bytes(evidence))
    identity = {
        "schema_version": "capture-intent/v1",
        "source_occurrence_id": source["source_occurrence_id"],
        "source_event_id": source["source_event_id"],
        "occurred_at": source["occurred_at"],
        "checkpoint_reason": source["checkpoint_reason"],
        "chunk_index": source["chunk_index"],
        "chunk_sha256": chunk_digest,
    }
    intent_id = sha256_bytes(canonical_json_bytes(identity))
    record = {
        "schema_version": "capture-intent/v1",
        "intent_id": intent_id,
        **dict(source),
        "complete_input_sha256": complete_digest,
        "chunk_sha256": chunk_digest,
    }
    validate_schema(record, SCRIPTS_DIR / "schemas" / "capture-intent-v1.json")
    encoded = canonical_json_bytes(record)
    if len(encoded) > MAX_CAPTURE_INTENT_BYTES:
        raise ValueError("capture intent exceeds its byte limit")
    return record, encoded


def _capture_relative_paths(intent_id: str) -> tuple[str, str]:
    shard = intent_id[:2]
    name = f"{intent_id}.json"
    pending = f"run/capture-intents/pending/{shard}/{name}"
    ready = f"run/capture-intents/ready/{shard}/{name}"
    return pending, ready


def _validate_capture_directory(path: Path, state_root: Path) -> None:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    unsafe = (
        path.is_symlink(),
        bool(getattr(info, "st_file_attributes", 0) & reparse),
        not stat.S_ISDIR(info.st_mode),
    )
    if any(unsafe):
        raise PermissionError("capture intent directory is unsafe")
    path.resolve(strict=True).relative_to(state_root.resolve(strict=True))


def _ensure_capture_directory(path: Path, state_root: Path) -> None:
    from reliable_memory import _harden_runtime_owner_only, fsync_directory

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        fsync_directory(path.parent)
    _validate_capture_directory(path, state_root)
    _harden_runtime_owner_only(path, 0o700)


def _ensure_capture_intent_directories(state_root: Path, intent_id: str) -> None:
    base = state_root / "run" / "capture-intents"
    paths = (
        base,
        base / "pending",
        base / "ready",
        base / "pending" / intent_id[:2],
        base / "ready" / intent_id[:2],
    )
    for path in paths:
        _ensure_capture_directory(path, state_root)


def _remove_verified_pending(path: Path, state_root: Path, expected_sha256: str) -> None:
    from reliable_memory import fsync_directory, read_runtime_bytes, sha256_bytes

    try:
        payload = read_runtime_bytes(
            path, state_root, max_bytes=MAX_CAPTURE_INTENT_BYTES, owner_only=True
        )
    except FileNotFoundError:
        return
    if sha256_bytes(payload) != expected_sha256:
        raise RuntimeError("pending capture intent digest changed")
    path.unlink()
    fsync_directory(path.parent)


@contextmanager
def _capture_publication_fence(queue: object, coordinator: object, intent_id: str):
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    try:
        fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
        try:
            yield owner, fence
        finally:
            coordinator.release_intent_fence(fence)
    finally:
        registry.release(owner)


def _publish_capture_files_and_task(
    queue: object,
    coordinator: object,
    *,
    intent_id: str,
    payload: bytes,
    intent_sha256: str,
    pending_relative: str,
    ready_relative: str,
) -> None:
    from reliable_memory import publish_runtime_file

    pending = Path(STATE_ROOT) / pending_relative
    ready = Path(STATE_ROOT) / ready_relative
    with _capture_publication_fence(queue, coordinator, intent_id) as (owner, fence):
        publish_runtime_file(
            pending, payload, state_root=Path(STATE_ROOT), create_only=True, mode=0o600
        )
        queue.index_capture_intent_pending(
            intent_id=intent_id,
            pending_path=pending_relative,
            ready_path=ready_relative,
            intent_sha256=intent_sha256,
            byte_size=len(payload),
        )
        publish_runtime_file(
            ready, payload, state_root=Path(STATE_ROOT), create_only=True, mode=0o600
        )
        queue.mark_capture_intent_ready(
            intent_id=intent_id,
            pending_path=pending_relative,
            ready_path=ready_relative,
            intent_sha256=intent_sha256,
            byte_size=len(payload),
        )
        _remove_verified_pending(pending, Path(STATE_ROOT), intent_sha256)
        queue.enqueue_capture_task_replay_safe(
            "flush",
            CAPTURE_HANDLER_VERSION,
            {
                "intent_id": intent_id,
                "intent_path": ready_relative,
                "intent_sha256": intent_sha256,
            },
            intent_id=intent_id,
            intent_path=ready_relative,
            intent_sha256=intent_sha256,
            capture_fence=fence,
            owner=owner,
        )


def _publish_durable_capture_intent(
    envelope: EventEnvelope,
    payload: Mapping[str, Any],
    slug: str | None,
    trigger: str | None,
) -> str | None:
    from markdown_transaction import active_markdown_coordinator
    from memory_queue import active_memory_queue
    from reliable_memory import sha256_bytes

    text = _capture_evidence_text(envelope, payload)
    if text is None:
        return None
    source = _capture_source_record(envelope, slug, trigger, text)
    record, encoded = _capture_intent_record(source)
    intent_id = str(record["intent_id"])
    intent_sha256 = sha256_bytes(encoded)
    pending_relative, ready_relative = _capture_relative_paths(intent_id)
    state_root = Path(STATE_ROOT).resolve(strict=True)
    _ensure_capture_intent_directories(state_root, intent_id)
    queue = active_memory_queue(Path(ROOT), state_root)
    coordinator = active_markdown_coordinator(Path(ROOT), state_root)
    _publish_capture_files_and_task(
        queue,
        coordinator,
        intent_id=intent_id,
        payload=encoded,
        intent_sha256=intent_sha256,
        pending_relative=pending_relative,
        ready_relative=ready_relative,
    )
    return intent_id


def _record_capture_intent(result: dict[str, Any], intent_id: str | None) -> None:
    if intent_id is not None:
        result["capture_intent_ids"] = [intent_id]


def _materialize_event_transcript(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> Path | None:
    text = envelope.payload.get("transcript_text")
    if not isinstance(text, str):
        return None
    if not text:
        return None
    path = _write_transient_transcript(envelope, text)
    payload["transcript_path"] = str(path)
    payload["ephemeral_transcript"] = True
    result["transcript_path"] = str(path)
    return path


def _cleanup_durable_transcript(path: Path | None, intent_id: str | None) -> None:
    if path is None:
        return
    if intent_id is None:
        return
    _cleanup_runtime_transient(path)


def _wake_capture_worker(result: dict[str, Any], intent_id: str | None) -> bool:
    if intent_id is None:
        return False
    try:
        process_id = spawn_detached(
            [
                sys.executable,
                str(SCRIPTS_DIR / "integration_adapter.py"),
                "--capture-worker",
            ]
        )
    except Exception:  # noqa: BLE001
        process_id = None
    started = process_id is not None
    result["flush_spawned"] = started
    return started


def _capture_precompact(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    intent_id: str | None,
) -> bool:
    if not payload.get("transcript_path"):
        result["heartbeat_recorded"] = _record_activity(envelope, slug, project_dir)
        return False
    return _wake_capture_worker(result, intent_id)


def _ingest_precompact(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    transient_path = _materialize_event_transcript(envelope, payload, result)
    intent_id = None
    try:
        intent_id = _publish_durable_capture_intent(
            envelope, payload, slug, _string(payload.get("trigger"))
        )
        _record_capture_intent(result, intent_id)
        _capture_precompact(
            envelope, payload, slug, project_dir, result, intent_id
        )
    finally:
        _cleanup_durable_transcript(transient_path, intent_id)


def _session_end_trigger(trigger: str | None, payload: Mapping[str, Any]) -> Any:
    if isinstance(trigger, str):
        safe_trigger = redact_secrets(trigger)
        if safe_trigger:
            return safe_trigger
    return payload.get("reason")


def _tag_session_end(
    payload: Mapping[str, Any], project_dir: Path | None, result: dict[str, Any]
) -> None:
    tagged = _run_delegate("session_end_project_tag.py", payload, project_dir=project_dir)
    result["daily_log_written"] = getattr(tagged, "returncode", 0) == 0
    result["returncode"] = getattr(tagged, "returncode", 0)


def _capture_session_end(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    intent_id: str | None,
) -> bool:
    if payload.get("transcript_path"):
        _tag_session_end(payload, project_dir, result)
        return _wake_capture_worker(result, intent_id)
    if force_stub:
        _tag_session_end(payload, project_dir, result)
        return False
    if slug and project_dir:
        result["heartbeat_recorded"] = _record_activity(envelope, slug, project_dir)
    return False


def _ingest_session_end(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    transient_path = _materialize_event_transcript(envelope, payload, result)
    payload["trigger"] = _session_end_trigger(trigger, payload)
    intent_id = None
    try:
        intent_id = _publish_durable_capture_intent(
            envelope, payload, slug, _string(payload.get("trigger"))
        )
        _record_capture_intent(result, intent_id)
        _capture_session_end(
            envelope, payload, slug, project_dir, result, force_stub, intent_id
        )
    finally:
        _cleanup_durable_transcript(transient_path, intent_id)


def ingest_event(
    envelope: EventEnvelope,
    *,
    force_stub: bool = False,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Apply shared lifecycle persistence policy to a normalized envelope."""
    _observe_checkpoint_fail_open(envelope)
    payload = _canonical_capture_payload(envelope)
    slug, project_dir = _project_context(envelope)
    result = _ingest_result(slug, payload)
    handlers = {
        "session_start": _ingest_session_start,
        "user_prompt": _ingest_user_prompt,
        "post_tool_use": _ingest_post_tool,
        "pre_compact": _ingest_precompact,
        "session_end": _ingest_session_end,
    }
    handler = handlers.get(envelope.event_type)
    if handler is not None:
        handler(envelope, payload, slug, project_dir, result, force_stub, trigger)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--event")
    parser.add_argument("--delegate")
    parser.add_argument("--checkpoint-type")
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--capture-worker", action="store_true")
    return parser


def _run_active_capture_worker_once() -> int:
    from flush_memory import process_new_capture, run_capture_worker_once
    from markdown_transaction import active_markdown_coordinator
    from memory_queue import active_memory_queue

    vault = Path(ROOT).resolve(strict=True)
    state_root = Path(STATE_ROOT).resolve(strict=True)
    queue = active_memory_queue(vault, state_root)
    coordinator = active_markdown_coordinator(vault, state_root)
    process_missing = partial(process_new_capture, queue, coordinator)
    run_capture_worker_once(
        queue,
        coordinator,
        process_missing=process_missing,
    )
    return 0


def _decode_stdin(data: bytes) -> str:
    if len(data) > MAX_STDIN_BYTES:
        raise ValueError("invalid integration event")
    return data.decode("utf-8")


def _read_stdin_bounded() -> str:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    data = stream.read(MAX_STDIN_BYTES + 1)
    if isinstance(data, bytes):
        return _decode_stdin(data)
    if len(data.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ValueError("invalid integration event")
    return data


def _read_hook_input() -> dict[str, Any]:
    text = _read_stdin_bounded()
    if not text:
        text = "{}"
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("invalid integration event")
    return raw


def _apply_checkpoint_arg(raw: dict[str, Any], checkpoint_type: str | None) -> dict[str, Any]:
    if not checkpoint_type:
        return raw
    projected = dict(raw)
    projected["checkpoint_type"] = checkpoint_type
    return projected


def _normalize_cli_event(source: str, event: str, raw: Mapping[str, Any]) -> EventEnvelope | None:
    if source in IDE_SOURCES:
        return normalize_host_event(source, event, raw)
    return normalize_occurrence_event(source, event, raw)


def _result_context(result: Mapping[str, Any]) -> str:
    context = result.get("context")
    if isinstance(context, str):
        return context
    return ""


def _cursor_output(event: str, result: Mapping[str, Any]) -> dict[str, object]:
    if event == "user_prompt":
        return {"continue": True}
    if event != "session_start":
        return {}
    context = _result_context(result)
    if context:
        return {"additional_context": context}
    return {}


def _antigravity_output(event: str, result: Mapping[str, Any]) -> dict[str, object]:
    if event == "stop":
        return {"decision": "stop"}
    if event != "session_start":
        return {}
    context = _result_context(result)
    if context:
        return {"injectSteps": [{"ephemeralMessage": context}]}
    return {}


def _legacy_output(
    source: str, event_type: str, result: dict[str, Any]
) -> dict[str, object] | None:
    if event_type != "session_start":
        return None
    if source != "claude":
        return result
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": result.get("context", ""),
        }
    }


def _success_output(
    source: str,
    requested_event: str,
    envelope: EventEnvelope,
    result: dict[str, Any],
) -> dict[str, object] | None:
    if source == "cursor":
        return _cursor_output(requested_event, result)
    if source == "antigravity":
        return _antigravity_output(requested_event, result)
    return _legacy_output(source, envelope.event_type, result)


def _neutral_host_output(source: str | None, event: str | None) -> dict[str, object] | None:
    outputs = {
        ("cursor", "user_prompt"): {"continue": True},
        ("antigravity", "stop"): {"decision": "stop"},
    }
    if source in IDE_SOURCES:
        return outputs.get((source, event), {})
    return None


def _delegate_forwards_stdout(name: str) -> bool:
    return name in {
        "session_start_context.py",
        "session_start_project_state.py",
        "user_prompt_capture.py",
    }


def _dispatch_cli_event(
    args: argparse.Namespace, envelope: EventEnvelope | None
) -> dict[str, object] | None:
    if envelope is None:
        return _neutral_host_output(args.source, args.event)
    if args.delegate == CAPTURE_DELEGATES.get(envelope.event_type):
        result = ingest_event(envelope)
        return _success_output(args.source, args.event, envelope, result)
    if args.delegate:
        _observe_checkpoint_fail_open(envelope)
        _run_delegate(
            args.delegate,
            _canonical_capture_payload(envelope),
            forward_stdout=_delegate_forwards_stdout(args.delegate),
        )
        return None
    result = ingest_event(envelope)
    return _success_output(args.source, args.event, envelope, result)


def _run_cli_event(args: argparse.Namespace) -> dict[str, object] | None:
    if not args.source:
        raise ValueError("invalid integration event")
    if not args.event:
        raise ValueError("invalid integration event")
    raw = _apply_checkpoint_arg(_read_hook_input(), args.checkpoint_type)
    envelope = _normalize_cli_event(args.source, args.event, raw)
    return _dispatch_cli_event(args, envelope)


def _args_neutral_output(args: argparse.Namespace | None) -> dict[str, object] | None:
    if args is None:
        return None
    return _neutral_host_output(args.source, args.event)


def main(argv: Sequence[str] | None = None) -> int:
    """Host-safe CLI: invalid input and capture failures never escape."""
    args: argparse.Namespace | None = None
    output: dict[str, object] | None = None
    try:
        args = _parser().parse_args(argv)
        if args.maintenance:
            return _run_session_start_maintenance()
        if args.capture_worker:
            return _run_active_capture_worker_once()
        output = _run_cli_event(args)
    except (Exception, SystemExit):  # noqa: BLE001
        print("integration_adapter: capture skipped", file=sys.stderr)
        output = _args_neutral_output(args)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
