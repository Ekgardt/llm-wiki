"""Deterministic breadcrumbs use the existing durable capture lifecycle."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

EVENTS = frozenset({"user_prompt", "post_tool_use"})


def is_breadcrumb(record) -> bool:
    return record.get("event") in EVENTS


def _source(event, slug, session, details, operation_id):
    from iso_time import local_now

    text = canonical_json_bytes(details).decode()
    return {
        "source_occurrence_id": operation_id,
        "source_event_id": operation_id,
        "occurred_at": local_now().isoformat(),
        "host": details.get("agent", "unknown"),
        "event": event,
        "session": session,
        "project_slug": slug,
        "worktree": None,
        "trigger": None,
        "checkpoint_reason": None,
        "chunk_index": 0,
        "chunk_count": 1,
        "evidence": [{"role": "breadcrumb", "parts": [{"type": "text", "text": text}]}],
    }


def _prior_payload(state_root, relative):
    from memory_state import MAX_CAPTURE_INTENT_BYTES
    from reliable_memory import read_runtime_bytes

    try:
        (state_root / relative).lstat()
        return read_runtime_bytes(
            state_root / relative, state_root, max_bytes=MAX_CAPTURE_INTENT_BYTES, owner_only=True
        )
    except FileNotFoundError:
        return None


def _reuse_record(source, encoded, state_root, pending, ready):
    prior = _prior_payload(state_root, ready)
    if prior is None:
        prior = _prior_payload(state_root, pending)
    if prior is None:
        return encoded
    return _verified_prior(source, prior)


def _verified_prior(source, prior):
    from flush_memory import _decode_capture_intent, _require_capture_intent_identity
    from integration_adapter import _encoded_capture_record

    record = _decode_capture_intent(prior)
    _require_capture_intent_identity(record)
    _, expected = _encoded_capture_record({**source, "occurred_at": record["occurred_at"]})
    if expected != prior:
        raise ValueError("breadcrumb occurrence is bound to different content")
    return prior


def _stage_before_database(source, state_root):
    """Keep the immutable input before any potentially contended database open."""
    from integration_adapter import (
        _capture_relative_paths,
        _encoded_capture_record,
        _ensure_capture_intent_directories,
    )
    from memory_state import MAX_CAPTURE_INTENT_BYTES
    from reliable_memory import publish_runtime_file, sync_runtime_directory

    record, encoded = _encoded_capture_record(source)
    if len(encoded) > MAX_CAPTURE_INTENT_BYTES:
        raise ValueError("breadcrumb intent exceeds the capture record byte limit")
    intent_id = record["intent_id"]
    pending, ready = _capture_relative_paths(intent_id)
    _ensure_capture_intent_directories(state_root, intent_id)
    retained = _reuse_record(source, None, state_root, pending, ready)
    if retained is not None:
        sync_runtime_directory((state_root / pending).parent)
        sync_runtime_directory((state_root / ready).parent)
        return intent_id
    try:
        publish_runtime_file(
            state_root / pending, encoded, state_root=state_root, create_only=True, mode=0o600,
        )
    except (OSError, RuntimeError, ValueError) as error:
        _require_concurrent_retention(source, state_root, pending, ready, error)
    return intent_id


def _require_concurrent_retention(source, state_root, pending, ready, error):
    from reliable_memory import sync_runtime_directory

    if _reuse_record(source, None, state_root, pending, ready) is None:
        raise error
    sync_runtime_directory((state_root / pending).parent)
    sync_runtime_directory((state_root / ready).parent)


def queue_breadcrumb(event, slug, session, details, operation_id) -> bool:
    """True means durably accepted; an unadopted vault keeps its v2 writer."""
    from integration_adapter import _wake_capture_worker
    from markdown_transaction import _reliability_v3_records_present
    from memory_state import STATE_ROOT

    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", STATE_ROOT)).resolve()
    if not _reliability_v3_records_present(state_root):
        return False
    source = _source(event, slug, session, details, operation_id or uuid.uuid4().hex)
    intent_id = _stage_before_database(source, state_root)
    _wake_capture_worker({}, intent_id)
    return True


def _legacy_recorded(root, coordinator, operation_id):
    """Keep a completed synchronous capture completed across the upgrade."""
    if operation_id is None:
        return False
    record = _legacy_operation_record(coordinator, operation_id)
    if record is None:
        return False
    return _legacy_marker_present(root, record, record.operation_id)


def _legacy_operation_record(coordinator, operation_id):
    record = coordinator._record_for_operation_id(operation_id)
    if record is not None:
        return record
    prefix, _, digest = operation_id.partition(":")
    if prefix not in {"user-prompt", "post-tool"} or re.fullmatch("[0-9a-f]{64}", digest) is None:
        return None
    return coordinator._record_for_operation_id(f"{prefix}:fallback:{digest}")


def _legacy_marker_present(root, record, operation_id):
    if len(record.operations) != 1:
        return False
    path = record.operations[0].path
    if not path.startswith("knowledge/daily/"):
        return False
    from daily_log_append import _carries

    return _carries(
        root / path, f"<!-- llm-wiki-operation:{sha256_bytes(operation_id.encode())} -->"
    )


def _details(record):
    evidence = record["evidence"]
    raw = evidence[0]["parts"][0]["text"]
    details = json.loads(raw)
    required = {"user_prompt": {"preview"}, "post_tool_use": {"preview", "tool", "agent"}}
    valid_shape = evidence == [{"role": "breadcrumb", "parts": [{"type": "text", "text": raw}]}]
    if not valid_shape or set(details) != required[record["event"]]:
        raise ValueError("invalid breadcrumb evidence")
    if not all(isinstance(value, str) for value in details.values()):
        raise ValueError("invalid breadcrumb fields")
    return details


def _heading(record, details, stamp):
    session = str(record["session"] or "unknown")[:8]
    slug = str(record["project_slug"] or "unknown")
    if record["event"] == "user_prompt":
        return f"- `[{stamp}] prompt | {session} | {slug}` "
    return f"- `[{stamp}] tool | {details['agent']} | {session} | {slug} | {details['tool']}` "


def _plan(record):
    from daily_log_append import contained_block
    from secret_redact import redact_secrets

    at = datetime.fromisoformat(str(record["occurred_at"]).replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("breadcrumb time requires a timezone")
    details = _details(record)
    text = contained_block(
        redact_secrets(_heading(record, details, at.strftime("%H:%M:%S")) + details["preview"])
    )
    marker = sha256_bytes(f"capture-markdown:{record['intent_id']}".encode())
    block = f"\n<!-- llm-wiki-operation:{marker} -->\n{text}\n"
    return [
        {
            "kind": "append",
            "path": f"knowledge/daily/{at:%Y-%m-%d}.md",
            "block": block,
            "block_sha256": sha256_bytes(block.encode()),
            "operation_id": f"capture-markdown:{record['intent_id']}",
            "chosen_at": at.isoformat(),
        }
    ]


def _decision(record, active):
    return {
        "schema_version": "capture-breadcrumb-decision/v1",
        "stage": "flush",
        "intent_id": record["intent_id"],
        "intent_sha256": active.intent_sha256,
        "complete_input_sha256": record["complete_input_sha256"],
        "chunk_sha256": record["chunk_sha256"],
        "renderer": "breadcrumb/v1",
        "outcome": "breadcrumb_written",
        "operation_plan": _plan(record),
        "processing_binding": {
            "kind": "task",
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
        },
    }


def require_decision(decision, record):
    expected = (
        "capture-breadcrumb-decision/v1",
        "breadcrumb/v1",
        "breadcrumb_written",
        _plan(record),
    )
    actual = (
        decision.get("schema_version"),
        decision.get("renderer"),
        decision.get("outcome"),
        decision.get("operation_plan"),
    )
    if actual != expected:
        raise RuntimeError("breadcrumb decision does not match its source")


def process_breadcrumb(queue, coordinator, lease, active, task_fence, intent_fence, owner, record):
    from flush_memory import (
        _complete_capture_decision,
        _ensure_capture_results_directory,
        _existing_capture_decision,
        _index_capture_decision,
    )

    args = (queue, coordinator, lease, active, task_fence, intent_fence, owner)
    _ensure_capture_results_directory(queue)
    resolved = _existing_capture_decision(*args, record)
    if resolved is None:
        decision = _decision(record, active)
        validate_schema(
            decision, Path(__file__).with_name("schemas") / "capture-breadcrumb-decision-v1.json"
        )
        indexed = _index_capture_decision(*args, canonical_json_bytes(decision))
        resolved = indexed, decision
    return _complete_capture_decision(*args, *resolved)
