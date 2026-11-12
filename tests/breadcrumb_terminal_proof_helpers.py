"""Helpers shared by test_breadcrumb_terminal_proof.py and the test files importing them."""
from __future__ import annotations

import json
from contextlib import contextmanager

import breadcrumb_decision
import breadcrumb_evidence
import capture_intent_records
import queue_core

from tests.breadcrumb_storage_helpers import _bundle, _publish
from tests.queue_v3_capture_links_helpers import _coordinator, _queue


@contextmanager
def _delivery(root):
    queue, coordinator = _queue(root), _coordinator(root)
    with _delivery_pair(queue, coordinator) as arguments:
        yield arguments


@contextmanager
def _delivery_pair(queue, coordinator):
    publication = _publish(queue, coordinator)
    bundle = _bundle(queue.state_root, publication.intent_id)
    lease = queue.claim_capture("proof-worker", handler_versions=(2,))
    with queue.queue_owner(role="queue-worker", scope="worker:proof-worker") as owner:
        with queue_core.capture_task_fences(
            queue, coordinator, lease.id, intent_id=publication.intent_id,
            mode="worker", owner=owner,
        ) as (task_fence, intent_fence):
            active = queue.active_capture_binding(None, lease.id)
            capture_intent_records.ensure_capture_results_directory(queue)
            encoded = breadcrumb_decision.decision_bytes(bundle)
            decision = capture_intent_records.index_capture_decision(
                queue, coordinator, lease, active, task_fence, intent_fence, owner, encoded,
            )
            yield (queue, coordinator, lease, task_fence, intent_fence, owner,
                   bundle, decision, json.loads(encoded))


def _committed(arguments):
    queue, coordinator, lease, _task, intent, owner, bundle, decision, record = arguments
    breadcrumb_evidence.publish_source(queue, coordinator, lease, intent, owner, bundle)
    active = queue.active_capture_binding(None, lease.id)
    _sealed, transaction = capture_intent_records.commit_capture_markdown(
        queue, coordinator, lease, active, intent, owner, decision, record,
    )
    return transaction


def _complete(arguments, disposition):
    queue, _coordinator, lease, task, intent, owner, _bundle_value, decision, _record = arguments
    active = queue.active_capture_binding(None, lease.id)
    return capture_intent_records.publish_capture_terminal(
        queue, lease, active, task, intent, owner, decision, disposition,
    )
