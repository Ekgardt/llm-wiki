"""Helpers shared by test_breadcrumb_worker.py and the test files importing them."""
from __future__ import annotations

import json
from functools import partial

import breadcrumb_evidence
import flush_memory
import integration_adapter
import llm_client
import memory_state
import pytest
from llm_wiki import event_project, integration_delegates

from tests.breadcrumb_storage_helpers import _bundle, _publish
from tests.queue_v3_capture_links_helpers import _coordinator, _queue


def _no_model(*args, **kwargs):
    raise AssertionError("breadcrumb delivery attempted a model call")


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    publication = _publish(queue, coordinator, {"prompt": "complete original-day evidence"})
    work = partial(
        flush_memory.run_capture_worker_once, queue, coordinator,
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
        handler_versions=(2,),
    )
    return queue, coordinator, publication, work


def _assert_complete(delivery):
    queue, _coordinator_value, publication, _work = delivery
    relative = f"run/queue-results/capture-{publication.intent_id}.json"
    terminal = json.loads((queue.state_root / relative).read_bytes())
    bundle = _bundle(queue.state_root, publication.intent_id)
    head = breadcrumb_evidence.source_path(bundle.anchor)
    assert breadcrumb_evidence.read_permanent_source(queue.vault, head) == bundle.content
    journal = queue.vault / "knowledge/daily/2026-09-29.md"
    assert journal.read_bytes().count(b"<!-- llm-wiki-operation:") == 1
    assert b"complete original-day evidence" in journal.read_bytes()
    assert queue.get(terminal["processing_binding"]["task_id"]).state == "succeeded"
    return terminal


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    """Yield (queue, coordinator) for `integration_adapter`, with its delegates stood in."""
    import markdown_transaction
    import project_checkpoint
    import queue_core

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(queue_core, "active_memory_queue", lambda *args: queue)
    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", lambda *args: coordinator)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda *args: None)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("fixture", tmp_path))
    monkeypatch.setattr(integration_delegates, "run_delegate", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_checkpoint, "observe_checkpoint_fail_open", lambda event: None)
    counter_state = {}
    monkeypatch.setattr(memory_state, "update_state", lambda mutate, **kwargs: mutate(counter_state))
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    return queue, coordinator


def _ingress_event(kind, **extra):
    payloads = {
        "user_prompt": {"prompt": "да"},
        "post_tool_use": {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    }
    return integration_adapter.normalize_occurrence_event("claude", kind, {
        "session_id": "host-session", "cwd": "/fixture", "event_id": "host-event",
        "timestamp": "2026-09-29T23:59:59+00:00", **payloads[kind], **extra,
    })
