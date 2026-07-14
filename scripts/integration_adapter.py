"""Normalize native host lifecycle events before existing capture pipelines."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
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
from session_start_context import build_context as build_session_start_context
from session_start_project_state import _compute_slug

SCRIPTS_DIR = Path(__file__).resolve().parent
DELEGATE_TIMEOUT_SECONDS = 10
MAINTENANCE_DRAIN_TIMEOUT_SECONDS = 600
MAX_TRANSCRIPT_TEXT_CHARS = 8000
MAX_CHECKPOINT_ERROR_CHARS = 500
TRANSIENT_CREATE_ATTEMPTS = 10
PENDING_CLAIM_SECONDS = 30.0
SOURCES = frozenset({"claude", "opencode", "codex"})
EVENTS = frozenset(
    {"session_start", "session_end", "pre_compact", "stop", "user_prompt", "post_tool_use"}
)
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
    }
)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str)), None)


def _safe_string(value: str | None) -> str | None:
    return redact_secrets(value) if value is not None else None


def _source_event_id(raw: Mapping[str, Any]) -> str | None:
    return _first_string(
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
    }.get(tool_name.lower(), tool_name)
    tool_input = raw.get("tool_input") if source != "opencode" else raw.get("input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    target = _first_string(
        tool_input.get("filePath"),
        tool_input.get("file_path"),
        tool_input.get("command"),
        raw.get("target"),
    ) or ""
    return {"tool_name": tool_name, "target": target}


def _canonical_delta_operation(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"id", "action", "value"}:
        raise ValueError("invalid project delta")
    item_id = value.get("id")
    action = value.get("action")
    text = value.get("value")
    if (
        not isinstance(item_id, str)
        or not 1 <= len(item_id) <= 256
        or action not in {"upsert", "close"}
        or not isinstance(text, str)
        or len(text) > 4096
    ):
        raise ValueError("invalid project delta")
    return {"id": item_id, "action": str(action), "value": text}


def _canonical_project_delta(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid project delta")
    scalar_names = ("goal", "phase", "current_task")
    list_names = (
        "next_actions",
        "decisions",
        "blockers",
        "changed_files",
        "commands",
        "verification",
    )
    operation_names = tuple(f"{name}_operations" for name in scalar_names)
    if set(value) - set(scalar_names + list_names + operation_names + ("legacy_context",)):
        raise ValueError("invalid project delta")
    canonical = _empty_delta()
    for name in scalar_names:
        if name in value:
            canonical[name] = _canonical_delta_operation(value[name])
    for name in operation_names + list_names:
        if name not in value:
            continue
        operations = value[name]
        if not isinstance(operations, list) or len(operations) > 10_000:
            raise ValueError("invalid project delta")
        canonical[name] = [_canonical_delta_operation(item) for item in operations]
    if "legacy_context" in value:
        legacy_context = value["legacy_context"]
        if not isinstance(legacy_context, str) or len(legacy_context) > 16384:
            raise ValueError("invalid project delta")
        canonical["legacy_context"] = legacy_context
    return canonical


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

    if event == "session_start":
        payload: dict[str, Any] = {
            "reason": _first_string(
                raw.get("reason"), raw.get("trigger"), raw.get("source")
            )
        }
    elif event == "stop":
        payload = {"reason": _first_string(raw.get("reason"), raw.get("trigger"))}
    elif event in {"session_end", "pre_compact"}:
        payload = {
            "reason": _first_string(raw.get("reason"), raw.get("trigger")),
            "transcript_path": _first_string(
                raw.get("transcript_path"),
                raw.get("transcriptPath"),
                raw.get("transcript"),
            ),
        }
        if "transcript_text" in raw:
            transcript_text = raw.get("transcript_text")
            if not isinstance(transcript_text, str) or len(transcript_text) > MAX_TRANSCRIPT_TEXT_CHARS:
                raise ValueError("invalid integration event")
            payload["transcript_text"] = transcript_text
    elif event == "user_prompt":
        payload = {"prompt": _string(raw.get("prompt"))}
    else:
        payload = _tool_payload(source, raw)

    for name in CHECKPOINT_SIGNAL_FIELDS:
        if name in raw:
            payload[name] = raw[name]
    if "project_delta" in payload:
        payload["project_delta"] = _canonical_project_delta(payload["project_delta"])
    if event in OCCURRENCE_EVENTS and isinstance(raw.get("occurrence_id"), str):
        payload["occurrence_id"] = raw["occurrence_id"]
    if event == "post_tool_use":
        if payload["tool_name"] in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
            payload.setdefault("changed", True)
            payload.setdefault("dirty", True)
            payload.setdefault("significant", True)
        if payload.get("checkpoint_type") == "significant_failure":
            payload["significant_failure"] = True
    if event == "session_start" and payload.get("reason") == "compact":
        payload["compaction_confirmed"] = True

    source_time = occurred_at or _parse_timestamp(raw.get("timestamp"))
    return build_event_envelope(
        event_type=event,
        payload=payload,
        occurred_at=source_time,
        captured_at=captured_at,
        agent=_safe_string(_string(raw.get("agent"))),
        session=_safe_string(_session(source, raw)),
        project=_safe_string(_string(raw.get("project"))),
        worktree=_safe_string(
            _first_string(raw.get("cwd"), raw.get("directory"), raw.get("projectRoot"))
        ),
        severity=_safe_string(_string(raw.get("severity"))),
        parent_event_id=_safe_string(_string(raw.get("parent_event_id"))),
        source_event_id=_safe_string(_source_event_id(raw)),
        redact=redact_secrets,
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
    if result.returncode == 0 and forward_stdout and _is_hook_output(result.stdout):
        sys.stdout.write(result.stdout)
    return result


def _is_hook_output(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    hook_output = parsed.get("hookSpecificOutput")
    return isinstance(hook_output, dict) and isinstance(
        hook_output.get("additionalContext"), str
    )


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
    if "occurrence_id" in payload:
        common["occurrence_id"] = payload["occurrence_id"]
    if envelope.event_type == "post_tool_use":
        common.update(
            {
                "tool_name": payload["tool_name"],
                "tool_input": {"filePath": payload["target"], "command": payload["target"]},
            }
        )
    elif envelope.event_type == "user_prompt":
        common["prompt"] = payload["prompt"]
    else:
        common.update(
            {
                "reason": payload.get("reason"),
                "transcript_path": payload.get("transcript_path"),
            }
        )
        if envelope.event_type == "pre_compact":
            common["trigger"] = payload.get("reason")
    for name in CHECKPOINT_SIGNAL_FIELDS | {"host_progress_signals"}:
        if name in payload:
            common[name] = payload[name]
    return common


def _checkpoint_observation(envelope: EventEnvelope) -> dict[str, object]:
    payload = envelope.payload
    event_type = str(payload.get("checkpoint_type") or envelope.event_type)
    if payload.get("compaction_confirmed") is True:
        event_type = "compaction_confirmed"
    elif isinstance(payload.get("token_percent"), (int, float)):
        event_type = "token_usage"
    else:
        for signal in (
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
        ):
            if payload.get(signal) is True:
                event_type = signal
                break
    if envelope.event_type == "post_tool_use" and event_type == "post_tool_use":
        if envelope.severity in {"error", "fatal"}:
            event_type = "significant_failure"
        elif payload.get("changed") is True and payload.get("significant") is True:
            event_type = "file_changed"
        elif payload.get("changed") is True:
            event_type = "mutation"
    observation: dict[str, object] = {
        "type": event_type,
        "event_id": envelope.event_id,
    }
    for name in ("dirty", "changed", "significant"):
        if name in payload:
            observation[name] = payload[name]
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
    raw_delta = envelope.to_dict()["payload"].get("project_delta")
    delta = dict(raw_delta) if isinstance(raw_delta, Mapping) else _empty_delta()
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": envelope.event_id,
        "idempotency_key": f"{envelope.event_id}:{reason}",
        "provenance": {
            "agent": envelope.agent or "unknown",
            "session": envelope.session or "unknown",
            "worktree": envelope.worktree or "unknown",
            "branch": str(envelope.payload.get("branch") or "unknown"),
            "source_event": envelope.source_event_id or envelope.event_id,
        },
        "trigger": str(_checkpoint_observation(envelope)["type"]),
        "reason": reason,
        "delta": delta,
        "evidence_event_ids": [envelope.event_id],
    }


def _pending_checkpoint(
    envelope: EventEnvelope, slug: str, state_key: str
) -> dict[str, object]:
    return {
        "event_id": envelope.event_id,
        "state_key": state_key,
        "occurred_at": envelope.occurred_at.isoformat(),
        "observation": _checkpoint_observation(envelope),
        "checkpoint_event": _checkpoint_event(envelope, slug, "pending"),
        "has_project_delta": isinstance(envelope.payload.get("project_delta"), Mapping),
    }


def _split_project_delta(delta: Mapping[str, object]) -> list[dict[str, object]]:
    scalar_names = ("goal", "phase", "current_task")
    list_limits = {
        "next_actions": 10,
        "decisions": 100,
        "blockers": 100,
        "changed_files": 100,
        "commands": 100,
        "verification": 100,
    }
    scalar_operations = {
        name: _scalar_delta_operations(delta, name) for name in scalar_names
    }
    chunk_count = max(
        [1]
        + [
            (len(operations) + 99) // 100
            for operations in scalar_operations.values()
        ]
        + [
            (len(delta[name]) + limit - 1) // limit
            for name, limit in list_limits.items()
            if isinstance(delta[name], list)
        ]
    )
    if chunk_count == 1:
        return [dict(delta)]
    chunks: list[dict[str, object]] = []
    for index in range(chunk_count):
        chunk = _empty_delta()
        if index == 0 and isinstance(delta.get("legacy_context"), str):
            chunk["legacy_context"] = delta["legacy_context"]
        for name, operations in scalar_operations.items():
            selected = operations[index * 100 : (index + 1) * 100]
            if selected:
                chunk[name] = selected[-1]
                chunk[f"{name}_operations"] = selected
        for name, limit in list_limits.items():
            operations = delta[name]
            assert isinstance(operations, list)
            chunk[name] = operations[index * limit : (index + 1) * limit]
        chunks.append(chunk)
    return chunks


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


def _release_pending_claims(queue_key: str, owner: str) -> None:
    def release(state: dict[str, Any]) -> None:
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


def _scalar_delta_operations(
    delta: Mapping[str, object], name: str
) -> list[dict[str, object]]:
    supplied = delta.get(f"{name}_operations")
    if isinstance(supplied, list) and supplied:
        return [dict(item) for item in supplied if isinstance(item, Mapping)]
    operation = delta[name]
    assert isinstance(operation, Mapping)
    if operation.get("id") == "checkpoint-none" and operation.get("action") == "close":
        return []
    return [dict(operation)]


def _bounded_pending_batch_count(items: Sequence[Mapping[str, object]]) -> int:
    list_limits = {"next_actions": 10}
    list_ids = {
        name: set()
        for name in (
            "next_actions",
            "decisions",
            "blockers",
            "changed_files",
            "commands",
            "verification",
        )
    }
    evidence: set[str] = set()
    scalar_counts = {name: 0 for name in ("goal", "phase", "current_task")}
    accepted = 0
    for item in items:
        next_evidence = evidence | {str(item["event_id"])}
        next_scalar_counts = dict(scalar_counts)
        next_list_ids = {name: set(values) for name, values in list_ids.items()}
        if _has_pending_delta(item):
            checkpoint = item["checkpoint_event"]
            assert isinstance(checkpoint, Mapping)
            delta = checkpoint["delta"]
            assert isinstance(delta, Mapping)
            for name in next_scalar_counts:
                next_scalar_counts[name] += len(
                    _scalar_delta_operations(delta, name)
                )
            for name, ids in next_list_ids.items():
                operations = delta[name]
                assert isinstance(operations, list)
                ids.update(
                    str(operation["id"])
                    for operation in operations
                    if isinstance(operation, Mapping)
                )
        if (
            len(next_evidence) > 100
            or any(count > 100 for count in next_scalar_counts.values())
            or any(len(ids) > list_limits.get(name, 100) for name, ids in next_list_ids.items())
        ):
            break
        evidence = next_evidence
        scalar_counts = next_scalar_counts
        list_ids = next_list_ids
        accepted += 1
    return max(1, accepted)


def _merge_pending_checkpoints(
    items: Sequence[Mapping[str, object]], decision: CheckpointDecision
) -> dict[str, object]:
    scalar_operations: dict[str, list[dict[str, object]]] = {
        name: [] for name in ("goal", "phase", "current_task")
    }
    list_operations: dict[str, dict[str, dict[str, object]]] = {
        name: {}
        for name in (
            "next_actions",
            "decisions",
            "blockers",
            "changed_files",
            "commands",
            "verification",
        )
    }
    evidence: list[str] = []
    legacy_context: list[str] = []
    for item in items:
        event_id = str(item["event_id"])
        if event_id not in evidence:
            evidence.append(event_id)
        if not _has_pending_delta(item):
            continue
        checkpoint = item["checkpoint_event"]
        assert isinstance(checkpoint, Mapping)
        delta = checkpoint["delta"]
        assert isinstance(delta, Mapping)
        context = delta.get("legacy_context")
        if isinstance(context, str) and context and context not in legacy_context:
            legacy_context.append(context)
        for name, operations in scalar_operations.items():
            operations.extend(_scalar_delta_operations(delta, name))
        for name, operations in list_operations.items():
            values = delta[name]
            assert isinstance(values, list)
            for operation in values:
                assert isinstance(operation, Mapping)
                item_id = str(operation["id"])
                operations.pop(item_id, None)
                operations[item_id] = dict(operation)

    merged_delta = _empty_delta()
    for name, operations in scalar_operations.items():
        if operations:
            merged_delta[name] = operations[-1]
            merged_delta[f"{name}_operations"] = operations
    for name, operations in list_operations.items():
        merged_delta[name] = list(operations.values())
    if legacy_context:
        merged_delta["legacy_context"] = "\n\n".join(legacy_context)[:16384]

    checkpoint = dict(items[-1]["checkpoint_event"])
    event_id = str(items[-1]["event_id"])
    checkpoint["occurrence_id"] = event_id
    checkpoint["idempotency_key"] = f"{event_id}:{decision.reason}"
    checkpoint["reason"] = decision.reason
    checkpoint["delta"] = merged_delta
    checkpoint["evidence_event_ids"] = evidence
    return checkpoint


def _drain_project_checkpoints(
    slug: str,
    queue_key: str,
    *,
    writer_wait_seconds: float | None = None,
) -> None:
    owner = f"{os.getpid()}:{secrets.token_hex(8)}"
    while True:
        claimed: list[tuple[list[dict[str, object]], dict[str, object]]] = []

        def claim(state: dict[str, Any]) -> None:
            pending = state.get("project_checkpoint_pending")
            if not isinstance(pending, dict):
                return
            queue = pending.get(queue_key)
            if not isinstance(queue, list) or not queue:
                return
            now = time.time()
            reducers = state.get("project_checkpoint_reducers")
            for item in queue:
                claim_until = item.get("claim_until")
                if (
                    item.get("claim_owner") not in {None, owner}
                    and isinstance(claim_until, (int, float))
                    and claim_until > now
                ):
                    return
            for item in queue:
                item["claim_owner"] = owner
                item["claim_until"] = now + PENDING_CLAIM_SECONDS
            claimed.append(
                (
                    [dict(item) for item in queue],
                    dict(reducers) if isinstance(reducers, dict) else {},
                )
            )

        update_state(claim, lock_timeout=0.5)
        if not claimed:
            return
        items, reducer_states = claimed[0]
        reducers: dict[str, CheckpointReducer] = {}
        decisions: list[CheckpointDecision | None] = []
        checkpoint_index: int | None = None
        checkpoint_decision: CheckpointDecision | None = None
        for index, item in enumerate(items):
            state_key = str(item["state_key"])
            if state_key not in reducers:
                reducer_state = reducer_states.get(state_key)
                reducers[state_key] = CheckpointReducer.from_state(
                    reducer_state if isinstance(reducer_state, Mapping) else None
                )
            observation = item["observation"]
            assert isinstance(observation, Mapping)
            occurred_at = datetime.fromisoformat(str(item["occurred_at"]))
            decision = reducers[state_key].observe(
                observation, now=occurred_at, commit=False
            )
            decisions.append(decision)
            if decision is not None and not decision.maintenance:
                checkpoint_index = index
                checkpoint_decision = decision
                break

        if checkpoint_index is None and any(_has_pending_delta(item) for item in items):
            latest = datetime.fromisoformat(str(items[-1]["occurred_at"]))
            due = any(
                _has_pending_delta(item)
                and (
                    reducers[str(item["state_key"])].last_checkpoint_at is None
                    or latest - reducers[str(item["state_key"])].last_checkpoint_at
                    >= timedelta(seconds=30)
                )
                for item in items
            )
            if due:
                checkpoint_index = len(items) - 1
                checkpoint_decision = CheckpointDecision(
                    "debounce_flush", checkpoint_at=latest
                )
            else:
                _release_pending_claims(queue_key, owner)
                return

        target_count = checkpoint_index + 1 if checkpoint_index is not None else len(items)
        flush_count = target_count
        if checkpoint_decision is not None:
            flush_count = min(
                target_count, _bounded_pending_batch_count(items[:target_count])
            )
            if flush_count < target_count:
                selected_time = datetime.fromisoformat(
                    str(items[flush_count - 1]["occurred_at"])
                )
                checkpoint_decision = CheckpointDecision(
                    "batch_flush", checkpoint_at=selected_time
                )
                reducers = {}
                decisions = []
                for item in items[:flush_count]:
                    state_key = str(item["state_key"])
                    if state_key not in reducers:
                        reducer_state = reducer_states.get(state_key)
                        reducers[state_key] = CheckpointReducer.from_state(
                            reducer_state
                            if isinstance(reducer_state, Mapping)
                            else None
                        )
                    observation = item["observation"]
                    assert isinstance(observation, Mapping)
                    decisions.append(
                        reducers[state_key].observe(
                            observation,
                            now=datetime.fromisoformat(str(item["occurred_at"])),
                            commit=False,
                        )
                    )
        selected = items[:flush_count]
        selected_decisions = decisions[:flush_count]
        if checkpoint_decision is not None and len(selected_decisions) < flush_count:
            selected_decisions.extend([None] * (flush_count - len(selected_decisions)))
        trigger_state_key = str(selected[-1]["state_key"])
        try:
            if checkpoint_decision is not None:
                checkpoint = _merge_pending_checkpoints(selected, checkpoint_decision)
                event_id = str(selected[-1]["event_id"])
                store = ProjectStore(ROOT, STATE_ROOT)
                if writer_wait_seconds is None:
                    store.checkpoint(
                        slug,
                        checkpoint,
                        f"lifecycle:{event_id[:16]}",
                    )
                else:
                    store.checkpoint(
                        slug,
                        checkpoint,
                        f"lifecycle:{event_id[:16]}",
                        writer_wait_seconds=writer_wait_seconds,
                    )
                reducers[trigger_state_key].commit_observation(
                    checkpoint_decision, outcome="checkpoint"
                )
            else:
                for item, decision in zip(selected, selected_decisions):
                    if decision is not None:
                        reducers[str(item["state_key"])].commit_observation(
                            decision, outcome="maintenance"
                        )
            committed_reducers = {
                state_key: reducer.to_state() for state_key, reducer in reducers.items()
            }
        except Exception:
            _release_pending_claims(queue_key, owner)
            raise

        def commit(state: dict[str, Any]) -> None:
            pending = state.setdefault("project_checkpoint_pending", {})
            queue = pending.setdefault(queue_key, [])
            if not queue:
                return
            expected_ids = [str(item["event_id"]) for item in selected]
            if [str(item.get("event_id")) for item in queue[:flush_count]] != expected_ids:
                raise RuntimeError("project checkpoint pending prefix changed")
            if any(item.get("claim_owner") != owner for item in queue[:flush_count]):
                raise RuntimeError("project checkpoint pending claim changed")
            reducers = state.setdefault("project_checkpoint_reducers", {})
            reducers.update(committed_reducers)
            del queue[:flush_count]
            for item in queue:
                if item.get("claim_owner") == owner:
                    item.pop("claim_owner", None)
                    item.pop("claim_until", None)
            if len(reducers) > 128:
                reducers.pop(next(iter(reducers)))

        try:
            update_state(commit, lock_timeout=0.5)
        except Exception:
            _release_pending_claims(queue_key, owner)
            raise


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
        reducers = state.get("project_checkpoint_reducers")
        reducer_state = reducers.get(state_key) if isinstance(reducers, dict) else None
        observed = (
            set(reducer_state.get("observed_event_ids", []))
            if isinstance(reducer_state, Mapping)
            else set()
        )
        pending = state.setdefault("project_checkpoint_pending", {})
        queue = pending.setdefault(slug, [])
        queued = {item.get("event_id") for item in queue}
        for pending_event in pending_events:
            event_id = pending_event["event_id"]
            if event_id not in observed and event_id not in queued:
                queue.append(pending_event)
                queued.add(event_id)

    update_state(enqueue, lock_timeout=0.5)
    _drain_project_checkpoints(
        slug, slug, writer_wait_seconds=writer_wait_seconds
    )


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
    if _is_reparse_point(path) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("transient directory is not secure")
    try:
        path.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("transient directory escaped state root") from exc
    if expected is not None and not _same_identity(info, expected):
        raise PermissionError("transient directory identity changed")
    return info


def _secure_transient_dir() -> tuple[Path, Path, os.stat_result]:
    state_root = Path(STATE_ROOT).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    current = state_root
    for name in ("cache", "transient-transcripts"):
        current = current / name
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = _validate_transient_dir(state_root, current)
    if os.name == "posix":
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise PermissionError("transient directory is not private")
        if mode != 0o700:
            current.chmod(0o700)
        info = _validate_transient_dir(state_root, current)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("transient directory is not private")
    else:
        _restrict_file_permissions(current)
        info = _validate_transient_dir(state_root, current, info)
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


def _write_posix_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    directory_fd = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    descriptor = -1
    opened: os.stat_result | None = None
    name: str | None = None
    succeeded = False
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or not _same_identity(
            directory_info, parent_info
        ):
            raise PermissionError("transient directory identity changed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _ in range(TRANSIENT_CREATE_ATTEMPTS):
            name = f"{envelope.event_id}-{secrets.token_hex(16)}.txt"
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("could not create unique transient transcript")

        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("transient file is not regular")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, text)
        os.fsync(descriptor)
        _validate_transient_dir(state_root, parent, directory_info)
        if not _same_file_at(directory_fd, name, opened):
            raise PermissionError("transient file identity changed")
        succeeded = True
        return parent / name
    finally:
        if not succeeded and opened is not None and name is not None:
            if _same_file_at(directory_fd, name, opened):
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _windows_path_from_fd(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
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
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    except (ImportError, OSError, ValueError):
        return None


def _write_windows_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    path: Path | None = None
    cleanup_path: Path | None = None
    opened: os.stat_result | None = None
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        path = parent / f"{envelope.event_id}-{secrets.token_hex(16)}.txt"
        try:
            descriptor = os.open(path, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("could not create unique transient transcript")

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("transient file is not regular")
        resolved = path.resolve(strict=True)
        resolved.relative_to(state_root)
        if resolved.parent != parent.resolve(strict=True) or not _same_file(path, opened):
            raise PermissionError("transient file containment changed")
        _restrict_file_permissions(path)
        if not _same_file(path, opened):
            raise PermissionError("transient file identity changed")
        _write_all(descriptor, text)
        os.fsync(descriptor)
        cleanup_path = _windows_path_from_fd(descriptor) or path
        _validate_transient_dir(state_root, parent, parent_info)
        if not _same_file(path, opened):
            raise PermissionError("transient file identity changed")
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if path is not None and opened is not None:
            _cleanup_created_transient(cleanup_path or path, opened)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _write_transient_transcript(envelope: EventEnvelope, text: str) -> Path:
    state_root, parent, parent_info = _secure_transient_dir()
    if os.name == "posix":
        return _write_posix_transient(
            envelope, text, state_root, parent, parent_info
        )
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


def _recover_project_handoff(slug: str | None, project_dir: Path | None) -> str:
    if not slug or project_dir is None:
        return ""
    try:
        store = ProjectStore(ROOT, STATE_ROOT)
        return recover_project_handoff(store, slug, project_root=project_dir).context
    except Exception as exc:  # noqa: BLE001
        _log_checkpoint_error(exc)
        return ""


def _append_context(context: str, handoff: str) -> str:
    if not handoff:
        return context
    if not context:
        return handoff
    return context.rstrip() + "\n\n" + handoff


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
    result: dict[str, Any] = {
        "slug": slug,
        "heartbeat_recorded": False,
        "daily_log_written": False,
        "flush_spawned": False,
        "transcript_path": payload.get("transcript_path"),
        "returncode": 0,
    }
    if envelope.event_type == "session_start":
        result["heartbeat_recorded"] = _record_activity(envelope, slug, project_dir)
        maintenance_pid = spawn_detached([
            sys.executable,
            str(SCRIPTS_DIR / "integration_adapter.py"),
            "--maintenance",
        ])
        result["maintenance_scheduled"] = maintenance_pid is not None
        result["context"] = _append_context(
            build_session_start_context(),
            _recover_project_handoff(slug, project_dir),
        )
    elif envelope.event_type == "user_prompt":
        _run_delegate("user_prompt_capture.py", payload, forward_stdout=True, project_dir=project_dir)
    elif envelope.event_type == "post_tool_use":
        _run_delegate("post_tool_capture.py", payload, project_dir=project_dir)
    elif envelope.event_type == "pre_compact":
        transient_path: Path | None = None
        ownership_transferred = False
        try:
            transcript_text = envelope.payload.get("transcript_text")
            if isinstance(transcript_text, str) and transcript_text:
                transient_path = _write_transient_transcript(envelope, transcript_text)
                payload["transcript_path"] = str(transient_path)
                payload["ephemeral_transcript"] = True
                result["transcript_path"] = str(transient_path)
            if payload.get("transcript_path"):
                captured = _run_delegate(
                    "precompact_capture.py", payload, project_dir=project_dir
                )
                ownership_transferred = _flush_started(captured)
                result["flush_spawned"] = ownership_transferred
                result["returncode"] = getattr(captured, "returncode", 0)
            else:
                result["heartbeat_recorded"] = _record_activity(
                    envelope, slug, project_dir
                )
        finally:
            if transient_path is not None and not ownership_transferred:
                _cleanup_runtime_transient(transient_path)
    elif envelope.event_type == "session_end":
        transient_path: Path | None = None
        ownership_transferred = False
        try:
            transcript_text = envelope.payload.get("transcript_text")
            if isinstance(transcript_text, str) and transcript_text:
                transient_path = _write_transient_transcript(envelope, transcript_text)
                payload["transcript_path"] = str(transient_path)
                payload["ephemeral_transcript"] = True
                result["transcript_path"] = str(transient_path)
            safe_trigger = redact_secrets(trigger) if isinstance(trigger, str) else None
            payload["trigger"] = safe_trigger or payload.get("reason")
            has_transcript = bool(payload.get("transcript_path"))
            if has_transcript or force_stub:
                tagged = _run_delegate(
                    "session_end_project_tag.py", payload, project_dir=project_dir
                )
                result["daily_log_written"] = getattr(tagged, "returncode", 0) == 0
                result["returncode"] = getattr(tagged, "returncode", 0)
            if has_transcript:
                captured = _run_delegate(
                    "session_end_capture.py", payload, project_dir=project_dir
                )
                ownership_transferred = _flush_started(captured)
                result["flush_spawned"] = ownership_transferred
                result["returncode"] = getattr(captured, "returncode", 0)
            elif not force_stub and slug and project_dir:
                result["heartbeat_recorded"] = _record_activity(
                    envelope, slug, project_dir
                )
        finally:
            if transient_path is not None and not ownership_transferred:
                _cleanup_runtime_transient(transient_path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--event")
    parser.add_argument("--delegate")
    parser.add_argument("--checkpoint-type")
    parser.add_argument("--maintenance", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Host-safe CLI: invalid input and capture failures never escape."""
    try:
        args = _parser().parse_args(argv)
        if args.maintenance:
            return _run_session_start_maintenance()
        if not args.source or not args.event:
            raise ValueError("invalid integration event")
        raw = json.loads(sys.stdin.read() or "{}")
        if not isinstance(raw, dict):
            raise ValueError("invalid integration event")
        if args.checkpoint_type:
            raw["checkpoint_type"] = args.checkpoint_type
        envelope = normalize_occurrence_event(args.source, args.event, raw)
        if args.delegate:
            _observe_checkpoint_fail_open(envelope)
            _run_delegate(
                args.delegate,
                _canonical_capture_payload(envelope),
                forward_stdout=args.delegate
                in {
                    "session_start_context.py",
                    "session_start_project_state.py",
                    "user_prompt_capture.py",
                },
            )
        else:
            result = ingest_event(envelope)
            if envelope.event_type == "session_start":
                print(json.dumps(result, ensure_ascii=False))
    except (Exception, SystemExit):  # noqa: BLE001
        print("integration_adapter: capture skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
