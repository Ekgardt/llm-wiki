"""A claimed success cannot replace durable destination evidence."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import breadcrumb_decision
import breadcrumb_evidence
import flush_memory
import memory_queue
import pytest
from reliable_memory import publish_runtime_file, sha256_bytes

from tests.test_breadcrumb_storage import _bundle, _publish
from tests.test_queue_v3_capture_links import _adopted_vault, _coordinator, _queue


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
        with memory_queue.capture_task_fences(
            queue, coordinator, lease.id, intent_id=publication.intent_id,
            mode="worker", owner=owner,
        ) as (task_fence, intent_fence):
            active = queue.active_capture_binding(None, lease.id)
            flush_memory._ensure_capture_results_directory(queue)
            encoded = breadcrumb_decision.decision_bytes(bundle)
            decision = flush_memory._index_capture_decision(
                queue, coordinator, lease, active, task_fence, intent_fence, owner, encoded,
            )
            yield (queue, coordinator, lease, task_fence, intent_fence, owner,
                   bundle, decision, json.loads(encoded))


def _committed(arguments):
    queue, coordinator, lease, _task, intent, owner, bundle, decision, record = arguments
    breadcrumb_evidence.publish_source(queue, coordinator, lease, intent, owner, bundle)
    active = queue.active_capture_binding(None, lease.id)
    _sealed, transaction = flush_memory._commit_capture_markdown(
        queue, coordinator, lease, active, intent, owner, decision, record,
    )
    return transaction


def _complete(arguments, disposition):
    queue, _coordinator, lease, task, intent, owner, _bundle_value, decision, _record = arguments
    active = queue.active_capture_binding(None, lease.id)
    return flush_memory._publish_capture_terminal(
        queue, lease, active, task, intent, owner, decision, disposition,
    )


@pytest.mark.parametrize("kind,error", [
    ("no_durable_content", "breadcrumb_terminal_unverified"),
    ("operator_discard", "capture_terminal_invalid"),
])
def test_unwritten_source_cannot_be_declared_disposable(tmp_path, kind, error):
    with _delivery(tmp_path) as arguments:
        disposition = {"kind": kind, "decision_sha256": arguments[7].decision_sha256}
        with pytest.raises(memory_queue.QueueOperationError, match=f"^{error}$"):
            _complete(arguments, disposition)
        assert arguments[0].get(arguments[2].id).state != "succeeded"
        assert _bundle(tmp_path, arguments[-1]["intent_id"]).content == arguments[6].content


@pytest.mark.parametrize("damage", ["source", "journal"])
def test_changed_destination_blocks_terminal_completion(tmp_path, damage):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        paths = {
            "source": arguments[-1]["source"]["path"],
            "journal": arguments[-1]["operation_plan"][0]["path"],
        }
        (tmp_path / paths[damage]).write_bytes(b"external change")
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb"):
            _complete(arguments, disposition)


def test_committed_complete_source_and_journal_can_complete(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition).startswith("run/queue-results/")
        assert arguments[0].get(arguments[2].id).state == "succeeded"


def test_terminal_cannot_name_a_nonexistent_transaction(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        disposition["transaction_id"] = "not-a-real-transaction"
        with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb"):
            _complete(arguments, disposition)


@pytest.mark.parametrize("kind,error", [
    ("no_durable_content", "breadcrumb_terminal_unverified"),
    ("operator_discard", "capture_terminal_invalid"),
])
def test_recovery_cannot_trust_a_preexisting_false_terminal(tmp_path, kind, error):
    with _delivery(tmp_path) as arguments:
        queue, _coordinator, lease, task, intent, owner, _bundle_value, decision, _record = arguments
        active = queue.active_capture_binding(None, lease.id)
        disposition = {"kind": kind, "decision_sha256": decision.decision_sha256}
        encoded = flush_memory._capture_terminal_bytes(active, decision, disposition)
        relative = f"run/queue-results/capture-{active.intent_id}.json"
        publish_runtime_file(tmp_path / relative, encoded, state_root=tmp_path, create_only=True)
        with pytest.raises(memory_queue.QueueOperationError, match=f"^{error}$"):
            queue.complete_existing_capture_terminal(
                lease, intent_id=active.intent_id, active_link_digest=active.active_digest,
                task_fence=task, intent_fence=intent, owner=owner,
            )
        assert sha256_bytes((tmp_path / relative).read_bytes()) == sha256_bytes(encoded)
        assert queue.get(lease.id).state != "succeeded"
        assert _bundle(tmp_path, active.intent_id).content == arguments[6].content


def test_separate_runtime_root_proves_the_actual_vault(tmp_path):
    import markdown_transaction

    vault, state = _adopted_vault(tmp_path)
    queue = memory_queue.active_memory_queue(vault, state)
    coordinator = markdown_transaction.active_markdown_coordinator(vault, state)
    with _delivery_pair(queue, coordinator) as arguments:
        transaction = _committed(arguments)
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition)
        assert (vault / arguments[-1]["source"]["path"]).is_file()
        assert not (state / arguments[-1]["source"]["path"]).exists()


def test_no_vault_context_cannot_guess_a_destination(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        arguments[0].vault = None
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb"):
            _complete(arguments, disposition)


def test_later_append_and_pruned_undo_do_not_invalidate_committed_evidence(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        journal = tmp_path / arguments[-1]["operation_plan"][0]["path"]
        journal.write_bytes(journal.read_bytes() + b"\n## Later entry\n\nlater evidence\n")
        arguments[1].prune(now=datetime.now(timezone.utc) + timedelta(days=3))
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition)


def test_completed_task_is_rechecked_before_cleanup(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        _complete(arguments, disposition)
        (tmp_path / arguments[-1]["source"]["path"]).unlink()
        queue, _coordinator_value, lease = arguments[:3]
        with queue.connection() as database:
            binding = queue.active_capture_binding(database, lease.id)
            with pytest.raises(memory_queue.QueueOperationError, match="breadcrumb"):
                queue._require_capture_terminal_proof(database, lease.id, binding)
