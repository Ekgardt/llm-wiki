"""Deterministic handler 2 on the existing fenced capture worker."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

import breadcrumb_decision
import breadcrumb_evidence
import breadcrumb_storage
import breadcrumb_terminal
import flush_memory
from reliable_memory import sha256_bytes


def _receipt(queue, coordinator, lease, active, fences, bundle):
    encoded = breadcrumb_decision.decision_bytes(bundle)
    indexed = queue.indexed_capture_decision(
        task_id=lease.id, intent_id=active.intent_id, stage="flush",
        active_link_digest=active.active_digest,
    )
    if indexed is not None and indexed.decision_sha256 != sha256_bytes(encoded):
        raise ValueError("breadcrumb receipt conflicts with its accepted input")
    held = flush_memory._held_by_this_task(
        queue, coordinator, lease, active, fences, indexed, encoded,
    )
    return held, json.loads(encoded)


def _prior_journal(coordinator, plan):
    operation_id = plan["operation_id"]
    with coordinator._connect() as database:
        rows = database.execute(
            'SELECT id, operation_id FROM "transaction" WHERE state=\'committed\' '
            'AND (operation_id=? OR operation_id GLOB ?)',
            (operation_id, operation_id + ":cas:*"),
        ).fetchall()
    records = [row for row in rows if breadcrumb_terminal._operation_id_matches(row["operation_id"], operation_id)]
    if len(records) > 1:
        raise ValueError("breadcrumb has multiple committed journal transactions")
    if not records:
        return None
    return coordinator._record(records[0]["id"])


def _journal(queue, coordinator, lease, intent_fence, owner, decision, record):
    active = queue.active_capture_binding(None, lease.id)
    plan = record["operation_plan"][0]
    prior = _prior_journal(coordinator, plan)
    if prior is None:
        return flush_memory._commit_capture_markdown(
            queue, coordinator, lease, active, intent_fence, owner, decision, record,
        )
    breadcrumb_terminal.require_journal_transaction(queue, active, plan, prior)
    return active, prior


def _deliver(queue, coordinator, lease, active, fences, bundle):
    task_fence, intent_fence, owner = fences
    flush_memory._ensure_capture_results_directory(queue)
    decision, record = _receipt(queue, coordinator, lease, active, fences, bundle)
    breadcrumb_evidence.publish_source(queue, coordinator, lease, intent_fence, owner, bundle)
    sealed, transaction = _journal(
        queue, coordinator, lease, intent_fence, owner, decision, record,
    )
    disposition = flush_memory._capture_markdown_disposition(transaction, decision)
    _project_checkpoint(bundle)
    return flush_memory._publish_capture_terminal(
        queue, lease, sealed, task_fence, intent_fence, owner, decision, disposition,
    )



def _checkpoint_envelope(bundle):
    """Restore a native occurrence, preserving its original identity and times.

    Generic breadcrumb evidence has no native event schema and no project
    checkpoint to reconstruct. A claimed native schema must validate fully.
    """
    from event_envelope import SCHEMA_VERSION, build_event_envelope

    event = json.loads(bundle.content)
    if "schema_version" not in event:
        return None
    _require_checkpoint_event(event, SCHEMA_VERSION)
    anchor = breadcrumb_storage.protocol.read_anchor(bundle.anchor)
    occurred = datetime.fromisoformat(anchor["occurred_at"])
    declared = {"host": occurred, "acceptance": None}[anchor["time_origin"]]
    envelope = build_event_envelope(
        event_type=event["event_type"], payload=event["payload"],
        occurred_at=declared, captured_at=datetime.fromisoformat(anchor["accepted_at"]),
        agent=event["agent"], session=event["session"], project=event["project"],
        worktree=event["worktree"], severity=event["severity"],
        parent_event_id=event["parent_event_id"], source_event_id=event["source_event_id"],
    )
    return replace(envelope, occurred_at=occurred)


def _require_checkpoint_event(event, version):
    fields = {"schema_version", "event_type", "payload", "agent", "session", "project",
              "worktree", "severity", "parent_event_id", "source_event_id"}
    if set(event) != fields or event["schema_version"] != version:
        raise ValueError("breadcrumb native checkpoint envelope is invalid")
    if event["event_type"] not in {"user_prompt", "post_tool_use"}:
        raise ValueError("breadcrumb native checkpoint event is invalid")


def _project_checkpoint(bundle):
    from integration_adapter import _observe_project_checkpoint

    envelope = _checkpoint_envelope(bundle)
    if envelope is not None:
        _observe_project_checkpoint(envelope)

def process_breadcrumb(queue, coordinator, lease, active, task_fence, intent_fence, owner):
    """The enclosing capture worker renews all authority; no model is called."""
    intent_id, _path, digest = flush_memory._capture_intent_reference(lease, active)
    bundle = breadcrumb_storage.load_bound_bundle(queue.state_root, intent_id, digest)
    return _deliver(queue, coordinator, lease, active, (task_fence, intent_fence, owner), bundle)
