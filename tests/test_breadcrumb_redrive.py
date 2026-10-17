"""A replacement task must preserve its predecessor's committed evidence."""
from __future__ import annotations

import json
from functools import partial

import flush_memory
import llm_client
import memory_queue
import pytest
from reliable_memory import canonical_json_bytes, publish_runtime_file

from tests.test_breadcrumb_terminal_proof import _committed, _delivery
from tests.test_breadcrumb_worker import _no_model


def _terminal_file(arguments, transaction):
    queue, _coordinator, lease = arguments[:3]
    active = queue.active_capture_binding(None, lease.id)
    disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
    content = flush_memory._capture_terminal_bytes(active, arguments[7], disposition)
    relative = f"run/queue-results/capture-{active.intent_id}.json"
    publish_runtime_file(queue.state_root / relative, content, state_root=queue.state_root, create_only=True)
    return content


@pytest.mark.parametrize("has_terminal", [False, True])
def test_replacement_task_keeps_parent_journal_and_terminal_bytes(tmp_path, monkeypatch, has_terminal):
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        terminal = None
        if has_terminal:
            terminal = _terminal_file(arguments, transaction)
        queue, coordinator, lease = arguments[:3]
        queue.fail(lease, memory_queue.QueueFailure("invalid_input"))
    child = queue.redrive(lease.id)
    journal = tmp_path / arguments[-1]["operation_plan"][0]["path"]
    before = journal.read_bytes()

    relative = flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )

    assert queue.get(child).state == "succeeded"
    assert journal.read_bytes() == before
    with queue.connection() as database:
        binding = queue.active_capture_binding(database, child)
        assert binding.seal_digest is not None
        assert queue._require_capture_terminal_proof(database, child, binding)
    if terminal is not None:
        assert (tmp_path / relative).read_bytes() == terminal


@pytest.mark.parametrize("field", ["task_id", "active_link_digest"])
def test_redrive_refuses_a_terminal_with_a_foreign_predecessor_binding(tmp_path, field):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        content = json.loads(_terminal_file(arguments, transaction))
        content["processing_binding"][field] = "0" * 64
        queue, coordinator, lease = arguments[:3]
        intent_id = queue.active_capture_binding(None, lease.id).intent_id
        target = tmp_path / f"run/queue-results/capture-{intent_id}.json"
        target.write_bytes(canonical_json_bytes(content))
        queue.fail(lease, memory_queue.QueueFailure("invalid_input"))
    child = queue.redrive(lease.id)

    with pytest.raises(memory_queue.QueueOperationError, match="capture_terminal_invalid"):
        flush_memory.run_capture_worker_once(
            queue, coordinator, handler_versions=(2,),
            process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
        )

    assert queue.get(child).state != "succeeded"
