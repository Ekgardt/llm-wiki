"""Project checkpoint capture: pending deltas, claims and backlog drain."""
from __future__ import annotations

import os
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from capture_diagnostics import record_hook_error
from event_envelope import EventEnvelope
from llm_wiki import event_project, runtime_roots
from memory_state import update_state
from project_journal import (
    SESSION_START_RECOVERY_SECONDS,
    CheckpointDecision,
    CheckpointReducer,
    ProjectStore,
)
from secret_redact import redact_secrets

# Basis unknown: value predates measurement; review when a checkpoint error is cut short.
MAX_CHECKPOINT_ERROR_CHARS = 500


# Basis unknown: value predates measurement; review when a live claim is taken over.
PENDING_CLAIM_SECONDS = 30.0
# How long a drain waits for the state lock. The hook path keeps 0.5 s: a
# session hook must not block on a contended file, and everything on that path
# is deliberately impatient (`SESSION_START_RECOVERY_SECONDS` is 0.25).
#
# That impatience is correct and it is also why a backlog cannot clear itself
# from a hook. Measured on this vault 2026-08-30: with `run/state.json` at
# 6.7 MB, hooks take and release the lock continuously and a 0.5 s drain loses
# every time — eight consecutive forced drains, each refused in 0.6 s, with the
# queue unmoved at 2 485. Recovery therefore belongs to the unattended nightly
# pass, which is allowed to wait.
PENDING_STATE_LOCK_SECONDS = 0.5
BACKLOG_STATE_LOCK_SECONDS = 10.0


# A bound on the recovery itself, so an unattended pass can never hang on it.
BACKLOG_DRAIN_SECONDS = 120.0


def string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


DELTA_SCALAR_NAMES = ("goal", "phase", "current_task")
DELTA_LIST_NAMES = (
    "next_actions",
    "decisions",
    "blockers",
    "changed_files",
    "commands",
    "verification",
)


DELTA_LIST_LIMITS = {
    "next_actions": 10,
    "decisions": 100,
    "blockers": 100,
    "changed_files": 100,
    "commands": 100,
    "verification": 100,
}


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


def _changed_observation_type(envelope: EventEnvelope, event_type: str) -> str:
    """What an unremarkable tool observation is called once it changed a file."""
    if envelope.payload.get("changed") is not True:
        return event_type
    if envelope.payload.get("significant") is True:
        return "file_changed"
    return "mutation"


def _tool_observation_type(envelope: EventEnvelope, event_type: str) -> str:
    if not _is_plain_tool_observation(envelope, event_type):
        return event_type
    if envelope.severity in {"error", "fatal"}:
        return "significant_failure"
    return _changed_observation_type(envelope, event_type)


def _copy_observation_flags(observation: dict[str, object], payload: Mapping[str, Any]) -> None:
    for name in ("dirty", "changed", "significant"):
        if name in payload:
            observation[name] = payload[name]


def checkpoint_observation(envelope: EventEnvelope) -> dict[str, object]:
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


def empty_delta() -> dict[str, object]:
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
    delta = checkpoint_delta(envelope)
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": envelope.event_id,
        "idempotency_key": f"{envelope.event_id}:{reason}",
        "provenance": {
            "agent": _known(envelope.agent),
            "session": _known(envelope.session),
            "worktree": _known(envelope.worktree),
            "branch": _known(string(envelope.payload.get("branch"))),
            "source_event": _known(envelope.source_event_id, envelope.event_id),
        },
        "trigger": str(checkpoint_observation(envelope)["type"]),
        "reason": reason,
        "delta": delta,
        "evidence_event_ids": [envelope.event_id],
    }


def _known(value: str | None, fallback: str = "unknown") -> str:
    if value:
        return value
    return fallback


# What a checkpoint records when nobody stated anything, which until now was
# nothing at all: 1 000 events in this vault's own journal, every one of them
# `checkpoint-none` closes and empty lists, so `state.md` read `Goal: None`
# and always had. `project_delta` is read here and written by no producer
# anywhere in `scripts/`, `integrations/`, `skills/` or `rules/` — only by two
# tests.
#
# What to derive, and from what, is settled by measurement rather than taste.
# The cold-start ablation separates the contribution of the agentic tasks in
# history from the agent's own response content and finds the tasks are the
# primary driver while the response content has little effect: what was done
# carries the signal, what was said about it does not. So the delta is derived
# from the observation, never narrated. An agent may still state a goal
# explicitly by sending `project_delta`, and that path is unchanged.
#
# Every derived value is dated, because the dominant failure of carried state
# is staleness rather than absence — a resuming agent treats the last session
# as current instead of re-validating it. A blocker that cannot say when it was
# opened is worse than an absent one.
#
# Research: `docs/research/2026-08-29-what-a-project-checkpoint-should-record.md`
_MAX_DERIVED_VALUE_CHARS = 200


def _derived_stamp(occurred_at: object) -> str:
    """Seconds are enough to judge staleness; microseconds are noise."""
    if isinstance(occurred_at, datetime):
        return occurred_at.isoformat(timespec="seconds")
    return str(occurred_at or "")


def _derived_value(text: str, occurred_at: object) -> str:
    """One item's value, dated, so a reader can see how old the claim is."""
    body = " ".join(str(text).split())[:_MAX_DERIVED_VALUE_CHARS]
    stamp = _derived_stamp(occurred_at)
    return f"{body} — {stamp}" if stamp else body


def _upsert(item_id: str, text: str, occurred_at: object) -> dict[str, str]:
    return {
        "id": item_id[:256],
        "action": "upsert",
        "value": _derived_value(text, occurred_at),
    }


def _derived_target(payload: Mapping[str, Any]) -> tuple[str, str]:
    tool = str(payload.get("tool_name") or "")
    return tool, str(payload.get("target") or "")


def _derived_changed_file(payload: Mapping[str, Any], at: object) -> list[dict[str, str]]:
    """A file this tool changed, keyed by its path so re-editing replaces it."""
    tool, target = _derived_target(payload)
    if payload.get("changed") is not True or not target or not tool:
        return []
    return [_upsert(f"file:{target}", target, at)]


def _derived_command(payload: Mapping[str, Any], at: object) -> list[dict[str, str]]:
    """A shell command, keyed by its own text so a repeat is one item."""
    tool, target = _derived_target(payload)
    if tool != "Bash" or not target:
        return []
    return [_upsert(f"cmd:{target[:120]}", target, at)]


def _derived_blocker(
    envelope: EventEnvelope, payload: Mapping[str, Any], at: object
) -> list[dict[str, str]]:
    """A failure the run actually hit, keyed by what failed rather than by when."""
    if envelope.severity not in {"error", "fatal"}:
        return []
    tool, target = _derived_target(payload)
    if not tool:
        return []
    return [_upsert(f"failed:{tool}:{target[:80]}", f"{tool} failed: {target}", at)]


def _derived_current_task(payload: Mapping[str, Any], at: object) -> dict[str, str]:
    """One id, always replaced: the newest thing the session was seen doing."""
    tool, target = _derived_target(payload)
    if not tool:
        return {"id": "checkpoint-none", "action": "close", "value": ""}
    return _upsert("observed", f"{tool} {target}".strip(), at)


def derived_delta(envelope: EventEnvelope) -> dict[str, object]:
    """The delta an observation supports on its own, with nothing narrated."""
    payload = envelope.payload
    at = getattr(envelope, "occurred_at", None)
    delta = empty_delta()
    delta["current_task"] = _derived_current_task(payload, at)
    delta["changed_files"] = _derived_changed_file(payload, at)
    delta["commands"] = _derived_command(payload, at)
    delta["blockers"] = _derived_blocker(envelope, payload, at)
    return delta


def checkpoint_delta(envelope: EventEnvelope) -> dict[str, object]:
    raw_delta = envelope.to_dict()["payload"].get("project_delta")
    if isinstance(raw_delta, Mapping):
        return dict(raw_delta)
    return derived_delta(envelope)


def pending_checkpoint(envelope: EventEnvelope, slug: str, state_key: str) -> dict[str, object]:
    return {
        "event_id": envelope.event_id,
        "state_key": state_key,
        "occurred_at": envelope.occurred_at.isoformat(timespec="microseconds"),
        "observation": checkpoint_observation(envelope),
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
    chunk = empty_delta()
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
    pending = pending_checkpoint(envelope, slug, state_key)
    delta = envelope.to_dict()["payload"].get("project_delta")
    if not isinstance(delta, Mapping):
        return [pending]
    chunks = _split_project_delta(delta)
    if len(chunks) == 1:
        return [pending]
    last = len(chunks) - 1
    return [
        _checkpoint_part(pending, f"{envelope.event_id}:part:{index + 1}", chunk, index == last)
        for index, chunk in enumerate(chunks)
    ]


def _checkpoint_part(
    pending: Mapping[str, Any], event_id: str, chunk: object, last: bool
) -> dict[str, object]:
    """One part of a split project delta; only the last carries the observation."""
    item = dict(pending)
    item["event_id"] = event_id
    item["has_project_delta"] = True
    checkpoint = dict(pending["checkpoint_event"])
    checkpoint["occurrence_id"] = event_id
    checkpoint["idempotency_key"] = f"{event_id}:pending"
    checkpoint["delta"] = chunk
    checkpoint["evidence_event_ids"] = [event_id]
    item["checkpoint_event"] = checkpoint
    item["observation"] = _part_observation(pending, event_id, last)
    return item


def _part_observation(pending: Mapping[str, Any], event_id: str, last: bool) -> dict[str, object]:
    if not last:
        return {"type": "coalesced_delta", "event_id": event_id}
    observation = dict(pending["observation"])
    observation["event_id"] = event_id
    return observation


def _release_claims(state: dict[str, Any], queue_key: str, owner: str) -> None:
    for item in _pending_queue(state, queue_key) or []:
        if item.get("claim_owner") == owner:
            item.pop("claim_owner", None)
            item.pop("claim_until", None)


def _release_pending_claims(
    queue_key: str,
    owner: str,
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
) -> None:
    def release(state: dict[str, Any]) -> None:
        _release_claims(state, queue_key, owner)

    update_state(release, lock_timeout=state_lock_seconds)


def _checkpoint_carries_delta(checkpoint: Mapping[str, object]) -> bool:
    delta = checkpoint.get("delta")
    if not isinstance(delta, Mapping):
        return False
    normalized = dict(delta)
    normalized.setdefault("current_task_operations", [])
    return normalized != empty_delta()


def has_pending_delta(item: Mapping[str, object]) -> bool:
    """Whether this pending item carries anything worth journalling.

    `has_project_delta` answers a narrower question than its name suggests: it
    records whether the *agent* supplied a `project_delta` in the payload. No
    hook ever does, so it is False on every event this system has ever seen —
    and asking it first threw away the delta `checkpoint_delta` had already
    derived from the observation itself.

    Measured 2026-09-05 on this vault: **3713 journal events across 71 projects,
    every one of them with an empty delta.** The tool, the file it changed, the
    command it ran and the failure it hit were all computed and then discarded
    here, which is why the layer meant to hand work between agents held nothing
    but timestamps. An outside reviewer found it before we did.

    The content decides. The flag is consulted only when there is no checkpoint
    event to look at.
    """
    checkpoint = item.get("checkpoint_event")
    if not isinstance(checkpoint, Mapping):
        return item.get("has_project_delta") is True
    return _checkpoint_carries_delta(checkpoint)


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
    if not has_pending_delta(item):
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
    merged = empty_delta()
    _copy_merged_scalars(merged, scalar_operations)
    for name, operations in list_operations.items():
        merged[name] = list(operations.values())
    if contexts:
        merged["legacy_context"] = "\n\n".join(contexts)[:16384]
    return merged


def batch_occurrence_id(evidence: Sequence[str]) -> str:
    """Name a batch after the whole batch, never after one of its members.

    It used to be `items[-1]["event_id"]`. The last member is not the batch:
    two batches ending at the same event but carrying different earlier events
    are two operations wearing one name, and a reservation refuses the second
    one forever. Measured on this vault 2026-08-30 — an outage on 08-28 left a
    reservation uncommitted, a later cycle formed a different batch ending at
    the same event, and the drain then failed identically on every attempt with
    2 464 checkpoints queued behind it.

    Each element of `evidence` is an event identifier that appears in the queue
    once and is deleted on commit, so identical membership means one operation
    retried — the case the reservation exists to collapse.
    See `docs/research/2026-08-30-a-batch-named-after-one-of-its-members.md`.
    """
    from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes

    return f"batch:{sha256_bytes(canonical_json_bytes(list(evidence)))}"


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
    occurrence_id = batch_occurrence_id(evidence)
    checkpoint.update(
        {
            "occurrence_id": occurrence_id,
            "idempotency_key": f"{occurrence_id}:{decision.reason}",
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


def claim_pending_state(
    state: dict[str, Any],
    queue_key: str,
    owner: str,
    claimed: list[tuple[list[dict[str, object]], dict[str, object], dict[str, object]]],
) -> None:
    queue = _pending_queue(state, queue_key)
    if not queue:
        return
    window = queue[:PENDING_CLAIM_WINDOW]
    if not _claim_queue(window, owner, time.time()):
        return
    inflight = dict(state.get(INFLIGHT_STATE_KEY, {}).get(queue_key) or {})
    claimed.append(([dict(item) for item in window], _copy_reducer_states(state), inflight))


def _claim_pending(
    queue_key: str,
    owner: str,
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]] | None:
    claimed: list[tuple[list[dict[str, object]], dict[str, object], dict[str, object]]] = []

    def claim(state: dict[str, Any]) -> None:
        claim_pending_state(state, queue_key, owner, claimed)

    update_state(claim, lock_timeout=state_lock_seconds)
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


def observe_pending_item(
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
        decision = observe_pending_item(item, reducers, reducer_states)
        decisions.append(decision)
        if _is_checkpoint_decision(decision):
            return reducers, decisions, index, decision
    return reducers, decisions, None, None


def _delta_due(
    item: Mapping[str, object],
    reducers: Mapping[str, CheckpointReducer],
    latest: datetime,
) -> bool:
    if not has_pending_delta(item):
        return False
    previous = reducers[str(item["state_key"])].last_checkpoint_at
    if previous is None:
        return True
    return latest - previous >= timedelta(seconds=30)


# How many events one project may hold back before the wait itself becomes the
# problem. The queue lives in `run/state.json`, and a reader refuses that file
# over 256 KiB: measured 2026-08-26, a single session held 136 events weighing
# 184 KiB, pushed the file to 262 KiB, and the scheduler and capture health
# checks reported nothing at all. Checkpointing early costs one extra journal
# entry; waiting costs every finding those two checks would have made.
MAX_PENDING_CHECKPOINT_ITEMS = 40


# How much of the queue one drain cycle claims, replays and rewrites. It used
# to be all of it, which made the cost of draining proportional to the backlog
# while the time allowed to pay it stayed `lock_timeout=0.5`. Measured on this
# vault 2026-08-30: a journal outage on 08-28 left 2 537 pending checkpoints,
# 3.7 MB inside a 6.7 MB `run/state.json`; the outage was repaired on 08-29 and
# the queue still did not move, because each cycle rewrote 4.8 MB three times
# and lost the lock — 1 338 `Could not acquire state lock` in one day. A window
# makes recovery linear in the backlog and never impossible. It is 100 because
# `_bounded_pending_batch_count` never accepts more than 100 evidence ids into
# one batch: a smaller window would change how many events a batch carries, and
# a larger one would only claim items no cycle can select.
# See `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`. Items past
# the window stay queued for the next cycle; nothing is dropped.
PENDING_CLAIM_WINDOW = 100


def debounce_due(
    items: Sequence[Mapping[str, object]],
    reducers: Mapping[str, CheckpointReducer],
    *,
    now: datetime | None = None,
) -> tuple[int | None, CheckpointDecision | None, bool]:
    """Flush the newest item when any pending delta is due, else keep waiting."""
    latest = datetime.fromisoformat(str(items[-1]["occurred_at"]))
    observed = max(latest, now or latest)
    due = _any_delta_due(items, reducers, observed) or len(items) >= (
        MAX_PENDING_CHECKPOINT_ITEMS
    )
    if due:
        return len(items) - 1, CheckpointDecision("debounce_flush", checkpoint_at=latest), False
    return None, None, True


def _resolve_debounce(
    items: Sequence[Mapping[str, object]],
    reducers: Mapping[str, CheckpointReducer],
    index: int | None,
    decision: CheckpointDecision | None,
    *,
    now: datetime | None = None,
) -> tuple[int | None, CheckpointDecision | None, bool]:
    if index is not None:
        return index, decision, False
    if not _any_pending_delta(items):
        return None, None, False
    return debounce_due(items, reducers, now=now)


def _any_pending_delta(items: Sequence[Mapping[str, object]]) -> bool:
    return any(has_pending_delta(item) for item in items)


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
    decisions = [observe_pending_item(item, reducers, reducer_states) for item in items]
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
    store = ProjectStore(runtime_roots.vault_root(), runtime_roots.state_root())
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


def commit_pending_state(
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
    state.get(INFLIGHT_STATE_KEY, {}).pop(queue_key, None)
    _release_claims(state, queue_key, owner)


def _commit_pending(
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    committed_reducers: Mapping[str, object],
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
) -> None:
    def commit(state: dict[str, Any]) -> None:
        commit_pending_state(state, queue_key, owner, selected, committed_reducers)

    update_state(commit, lock_timeout=state_lock_seconds)


def _persist_or_release(
    slug: str,
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    decisions: Sequence[CheckpointDecision | None],
    reducers: Mapping[str, CheckpointReducer],
    checkpoint_decision: CheckpointDecision | None,
    writer_wait_seconds: float | None,
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
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
        _release_pending_claims(queue_key, owner, state_lock_seconds)
        raise


def _commit_or_release(
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    committed_reducers: Mapping[str, object],
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
) -> None:
    try:
        _commit_pending(queue_key, owner, selected, committed_reducers, state_lock_seconds)
    except Exception:
        _release_pending_claims(queue_key, owner, state_lock_seconds)
        raise


# The batch a drain is about to write, recorded before the journal write so a retry after
# a failed commit replays that batch instead of planning a larger one. See
# `docs/research/2026-09-14-a-retried-checkpoint-is-the-same-batch.md`.
INFLIGHT_STATE_KEY = "project_checkpoint_inflight"


def _checkpoint_plan(
    items: list[dict[str, object]],
    reducer_states: dict[str, object],
    inflight: Mapping[str, object],
    *,
    now: datetime | None = None,
):
    """The batch to write: the in-flight one when it still heads the queue, else a fresh plan."""
    replayed = inflight_plan(items, reducer_states, inflight)
    if replayed is not None:
        return replayed
    reducers, decisions, index, decision = _observe_until_checkpoint(items, reducer_states)
    index, decision, waiting = _resolve_debounce(items, reducers, index, decision, now=now)
    if waiting:
        return None
    return _batch_plan(items, reducer_states, reducers, decisions, index, decision)


def inflight_plan(
    items: list[dict[str, object]],
    reducer_states: dict[str, object],
    inflight: Mapping[str, object],
):
    event_ids = inflight.get("event_ids")
    if not isinstance(event_ids, list) or not event_ids:
        return None
    if [str(item.get("event_id")) for item in items[: len(event_ids)]] != event_ids:
        return None
    selected = list(items[: len(event_ids)])
    reducers, decisions = _observe_all(selected, reducer_states)
    decision = CheckpointDecision(str(inflight.get("reason")), checkpoint_at=inflight_time(inflight))
    return selected, reducers, decisions, decision


def inflight_time(inflight: Mapping[str, object]) -> datetime | None:
    value = inflight.get("checkpoint_at")
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value)


def record_inflight_state(
    state: dict[str, Any],
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    decision: CheckpointDecision,
) -> None:
    _validate_pending_commit(state.setdefault("project_checkpoint_pending", {}).get(queue_key, []), selected, owner)
    checkpoint_at = decision.checkpoint_at.isoformat(timespec="microseconds") if decision.checkpoint_at is not None else None
    state.setdefault(INFLIGHT_STATE_KEY, {})[queue_key] = {
        "event_ids": [str(item["event_id"]) for item in selected],
        "reason": decision.reason,
        "checkpoint_at": checkpoint_at,
    }


def _record_inflight_or_release(
    queue_key: str,
    owner: str,
    selected: Sequence[Mapping[str, object]],
    decision: CheckpointDecision | None,
    state_lock_seconds: float,
) -> None:
    if decision is None:
        return
    try:
        update_state(
            lambda state: record_inflight_state(state, queue_key, owner, selected, decision),
            lock_timeout=state_lock_seconds,
        )
    except Exception:
        _release_pending_claims(queue_key, owner, state_lock_seconds)
        raise


def _drain_project_checkpoint_once(
    slug: str,
    queue_key: str,
    owner: str,
    writer_wait_seconds: float | None,
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
    *,
    now: datetime | None = None,
) -> bool:
    claimed = _claim_pending(queue_key, owner, state_lock_seconds)
    if claimed is None:
        return False
    plan = _checkpoint_plan(*claimed, now=now)
    if plan is None:
        _release_pending_claims(queue_key, owner, state_lock_seconds)
        return False
    selected, reducers, decisions, decision = plan
    _record_inflight_or_release(queue_key, owner, selected, decision, state_lock_seconds)
    committed = _persist_or_release(
        slug,
        queue_key,
        owner,
        selected,
        decisions,
        reducers,
        decision,
        writer_wait_seconds,
        state_lock_seconds,
    )
    _commit_or_release(queue_key, owner, selected, committed, state_lock_seconds)
    return True


def drain_project_checkpoints(
    slug: str,
    queue_key: str,
    *,
    writer_wait_seconds: float | None = None,
    state_lock_seconds: float = PENDING_STATE_LOCK_SECONDS,
    deadline: float | None = None,
    now: datetime | None = None,
) -> None:
    owner = f"{os.getpid()}:{secrets.token_hex(8)}"
    while _drain_project_checkpoint_once(
        slug, queue_key, owner, writer_wait_seconds, state_lock_seconds, now=now
    ):
        if deadline is not None and time.monotonic() >= deadline:
            return


def pending_backlog_slugs() -> list[str]:
    from memory_state import load_state

    pending = load_state().get("project_checkpoint_pending")
    if not isinstance(pending, dict):
        return []
    return sorted(slug for slug, queue in pending.items() if isinstance(queue, list) and queue)


def pending_backlog_depth(slug: str) -> int:
    from memory_state import load_state

    pending = load_state().get("project_checkpoint_pending")
    if not isinstance(pending, dict):
        return 0
    queue = pending.get(slug)
    return len(queue) if isinstance(queue, list) else 0


def drain_pending_backlog(budget_seconds: float = BACKLOG_DRAIN_SECONDS) -> dict[str, object]:
    """Clear whatever the impatient hook path could not, without a hook's clock.

    A hook drains with `PENDING_STATE_LOCK_SECONDS` and must: it runs while a
    person waits. That is exactly why a backlog survives it — measured on this
    vault 2026-08-30, eight consecutive forced drains each lost the lock in
    0.6 s and the queue stayed at 2 485. This runs unattended, waits properly,
    and stops at `budget_seconds` so it can never hang the pass that calls it.

    See `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`.
    """
    deadline = time.monotonic() + budget_seconds
    drained: dict[str, object] = {}
    failed: dict[str, str] = {}
    for slug in pending_backlog_slugs():
        drained[slug] = _drain_one_backlog(slug, deadline, failed)
        if time.monotonic() >= deadline:
            break
    return {"drained": drained, "failed": failed, "remaining": pending_backlog_slugs()}


def _drain_one_backlog(slug: str, deadline: float, failed: dict[str, str]) -> int:
    """One project's backlog, isolated: its failure is not the pass's failure.

    Measured 2026-08-30: a single unrecoverable reservation in one project raised
    out of the drain and stopped every other project behind it, and the orphan
    sweep after it never ran at all. A recovery pass that one bad row can halt
    is not a recovery pass.
    """
    before = pending_backlog_depth(slug)
    try:
        drain_project_checkpoints(
            slug,
            slug,
            state_lock_seconds=BACKLOG_STATE_LOCK_SECONDS,
            deadline=deadline,
            now=datetime.now(UTC),
        )
    except Exception as error:  # noqa: BLE001
        failed[slug] = bounded_checkpoint_error(error)
    return before - pending_backlog_depth(slug)


def observe_project_checkpoint(
    envelope: EventEnvelope,
    *,
    writer_wait_seconds: float | None = None,
) -> None:
    """Durably enqueue one envelope and drain its project's ordered queue."""
    slug, project_dir = event_project.project_context(envelope)
    if not slug or project_dir is None:
        return
    session_key = envelope.session or "unknown"
    state_key = f"{slug}:{session_key}"
    pending_events = _pending_checkpoints(envelope, slug, state_key)

    def enqueue(state: dict[str, Any]) -> None:
        enqueue_pending_events(state, state_key, slug, pending_events)

    update_state(enqueue, lock_timeout=0.5)
    drain_project_checkpoints(slug, slug, writer_wait_seconds=writer_wait_seconds)


def _observed_event_ids(state: Mapping[str, Any], state_key: str) -> set[object]:
    reducers = state.get("project_checkpoint_reducers")
    if not isinstance(reducers, dict):
        return set()
    reducer_state = reducers.get(state_key)
    if not isinstance(reducer_state, Mapping):
        return set()
    return set(reducer_state.get("observed_event_ids", []))


def enqueue_pending_events(
    state: dict[str, Any],
    state_key: str,
    slug: str,
    pending_events: Sequence[dict[str, object]],
) -> None:
    observed = _observed_event_ids(state, state_key)
    pending = state.setdefault("project_checkpoint_pending", {})
    expire_pending_events(pending)
    queue = pending.setdefault(slug, [])
    queued = {item.get("event_id") for item in queue}
    for pending_event in pending_events:
        event_id = pending_event["event_id"]
        if event_id not in observed and event_id not in queued:
            queue.append(pending_event)
            queued.add(event_id)


# A pending checkpoint event drains only when its project commits. A project
# that never commits again — every directory the slug rule now refuses — kept
# its events forever: 168 events for 91 projects, 232 KiB of `run/state.json`
# on 2026-09-23. Thirty days is longer than any lease, retry or nightly gap.
PENDING_EVENT_MAX_AGE = timedelta(days=30)


def _pending_event_expired(item: Mapping[str, object], cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(item.get("occurred_at"))) < cutoff
    except ValueError:
        return True


def _newest_pending_instant(pending: Mapping[str, Sequence[Mapping[str, object]]]) -> datetime | None:
    """The vault's own clock: the newest event any project still holds."""
    instants = []
    for queue in pending.values():
        instants.extend(_parsed_instant(item) for item in queue)
    return max((instant for instant in instants if instant is not None), default=None)


def _parsed_instant(item: Mapping[str, object]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(item.get("occurred_at")))
    except ValueError:
        return None


def expire_pending_events(pending: dict[str, list[dict[str, object]]]) -> None:
    """Drop events older than the bound and say so in the failure trail.

    Age is measured against the newest pending event, not the wall clock: a
    vault that has been quiet is not one whose memory has expired.
    """
    newest = _newest_pending_instant(pending)
    if newest is None:
        return
    cutoff = newest - PENDING_EVENT_MAX_AGE
    for slug in list(pending):
        _expire_one_queue(pending, slug, cutoff)


def _expire_one_queue(
    pending: dict[str, list[dict[str, object]]], slug: str, cutoff: datetime
) -> None:
    queue = pending[slug]
    kept = [item for item in queue if not _pending_event_expired(item, cutoff)]
    dropped = len(queue) - len(kept)
    if dropped:
        note_expired_events(slug, dropped)
    if kept:
        pending[slug] = kept
        return
    pending.pop(slug, None)


def note_expired_events(slug: str, dropped: int) -> None:
    try:
        from capture_diagnostics import record_capture_failure

        record_capture_failure(
            "checkpoint_expired",
            f"{dropped} pending checkpoint event(s) older than {PENDING_EVENT_MAX_AGE.days} days dropped",
            slug=slug,
        )
    except Exception:  # noqa: BLE001 - the trail never breaks a hook
        return


def bounded_checkpoint_error(error: BaseException) -> str:
    message = redact_secrets(f"{type(error).__name__}: {error}")
    return " ".join(message.split())[:MAX_CHECKPOINT_ERROR_CHARS]


def checkpoint_log_kind(error: BaseException) -> str:
    """A lost race is retried by the next session; a failure is not.

    Both used to be written as `project checkpoint:` and counted together, so
    the health check read six hundred retries as six hundred failures and said
    "still happening" on a vault where nothing was going wrong. Naming them
    apart costs one word and makes the count mean something again. The
    exception decides, by type and code (#26.3), not its text.
    """
    from capture_diagnostics import is_contention

    if is_contention(error):
        return "project checkpoint contention"
    return "project checkpoint"


def log_hook_error(kind: str, message: str) -> None:
    """One line in the hook failure log; a hook never fails its host over diagnostics."""
    record_hook_error(runtime_roots.state_root(), kind, message)


def log_checkpoint_error(error: BaseException) -> None:
    """Best-effort bounded diagnostics for fail-open lifecycle capture."""
    log_hook_error(checkpoint_log_kind(error), bounded_checkpoint_error(error))


def observe_checkpoint_fail_open(envelope: EventEnvelope) -> None:
    try:
        if envelope.event_type == "session_start":
            observe_project_checkpoint(
                envelope, writer_wait_seconds=SESSION_START_RECOVERY_SECONDS
            )
            return
        # The host waits for every synchronous hook. Try the writer once;
        # the durable checkpoint stays pending for the backlog recovery actor.
        observe_project_checkpoint(envelope, writer_wait_seconds=0.0)
    except Exception as exc:  # noqa: BLE001
        log_checkpoint_error(exc)

