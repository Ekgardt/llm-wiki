"""A claimed success cannot replace durable destination evidence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import capture_intent_records
import pytest
import queue_core
from reliable_memory import publish_runtime_file, sha256_bytes

from tests.breadcrumb_storage_helpers import _bundle
from tests.breadcrumb_terminal_proof_helpers import _committed, _complete, _delivery, _delivery_pair
from tests.queue_v3_capture_links_helpers import _adopted_vault


@pytest.mark.parametrize("kind,error", [
    ("no_durable_content", "breadcrumb_terminal_unverified"),
    ("operator_discard", "capture_terminal_invalid"),
])
def test_unwritten_source_cannot_be_declared_disposable(tmp_path, kind, error):
    with _delivery(tmp_path) as arguments:
        disposition = {"kind": kind, "decision_sha256": arguments[7].decision_sha256}
        with pytest.raises(queue_core.QueueOperationError, match=f"^{error}$"):
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
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        with pytest.raises(queue_core.QueueOperationError, match="breadcrumb"):
            _complete(arguments, disposition)


def test_committed_complete_source_and_journal_can_complete(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition).startswith("run/queue-results/")
        assert arguments[0].get(arguments[2].id).state == "succeeded"


def test_terminal_cannot_name_a_nonexistent_transaction(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        disposition["transaction_id"] = "not-a-real-transaction"
        with pytest.raises(queue_core.QueueOperationError, match="breadcrumb"):
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
        encoded = capture_intent_records.capture_terminal_bytes(active, decision, disposition)
        relative = f"run/queue-results/capture-{active.intent_id}.json"
        publish_runtime_file(tmp_path / relative, encoded, state_root=tmp_path, create_only=True)
        with pytest.raises(queue_core.QueueOperationError, match=f"^{error}$"):
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
    queue = queue_core.active_memory_queue(vault, state)
    coordinator = markdown_transaction.active_markdown_coordinator(vault, state)
    with _delivery_pair(queue, coordinator) as arguments:
        transaction = _committed(arguments)
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition)
        assert (vault / arguments[-1]["source"]["path"]).is_file()
        assert not (state / arguments[-1]["source"]["path"]).exists()


def test_no_vault_context_cannot_guess_a_destination(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        arguments[0].vault = None
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        with pytest.raises(queue_core.QueueOperationError, match="breadcrumb"):
            _complete(arguments, disposition)


def test_later_append_and_pruned_undo_do_not_invalidate_committed_evidence(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        journal = tmp_path / arguments[-1]["operation_plan"][0]["path"]
        journal.write_bytes(journal.read_bytes() + b"\n## Later entry\n\nlater evidence\n")
        arguments[1].prune(now=datetime.now(UTC) + timedelta(days=3))
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition)


def _expect_breadcrumb_terminal_proof_refused(queue, lease):
    with queue.connection() as database:
        binding = queue.active_capture_binding(database, lease.id)
        with pytest.raises(queue_core.QueueOperationError, match="breadcrumb"):
            queue._require_capture_terminal_proof(database, lease.id, binding)


def test_completed_task_is_rechecked_before_cleanup(tmp_path):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        disposition = capture_intent_records.capture_markdown_disposition(transaction, arguments[7])
        _complete(arguments, disposition)
        (tmp_path / arguments[-1]["source"]["path"]).unlink()
        queue, _coordinator_value, lease = arguments[:3]
        _expect_breadcrumb_terminal_proof_refused(queue, lease)
