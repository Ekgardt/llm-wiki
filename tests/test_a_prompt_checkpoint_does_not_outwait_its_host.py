"""Durable prompt capture must not wait a second writer's lease for its handoff."""
import threading
import time

import integration_adapter as adapter
import markdown_transaction
import memory_state
import pytest
from project_journal import ProjectStore

from tests.adopted_capture_vault import adopted_capture_vault
from tests.slow_machine import LONG_TIMEOUT

pytestmark = pytest.mark.shipped_append_budgets


def _hold_writer(coordinator, held, release):
    with coordinator.writer_gate():
        held.set()
        assert release.wait(LONG_TIMEOUT)


def _configure(tmp_path, monkeypatch):
    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run/state.json")
    monkeypatch.setattr(adapter, "_project_context", lambda event: ("demo", project))
    coordinator = markdown_transaction.active_markdown_coordinator(adapter.ROOT, state_root)
    return state_root, project, coordinator


def test_prompt_keeps_its_checkpoint_pending_without_waiting_past_host(tmp_path, monkeypatch):
    state_root, project, coordinator = _configure(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_writer, args=(coordinator, held, release))

    def wake(_result, _intent_id):
        writer.start()
        assert held.wait(LONG_TIMEOUT)
        return False

    monkeypatch.setattr(adapter, "_wake_capture_worker", wake)
    envelope = adapter.normalize_occurrence_event("claude", "user_prompt", {
        "prompt": "continue", "cwd": str(project), "session_id": "prompt-gate",
        "event_id": "prompt-gate", "task_completed": True,
    })
    started = time.monotonic()
    try:
        result = adapter.ingest_event(envelope)
        elapsed = time.monotonic() - started
        assert result["capture_durable"] is True
        assert elapsed < adapter.HOST_HOOK_TIMEOUT_SECONDS
        assert memory_state.load_state()["project_checkpoint_pending"]["demo"]
    finally:
        release.set()
        writer.join(LONG_TIMEOUT)
    assert not writer.is_alive()
    assert adapter.drain_pending_backlog()["failed"] == {}
    assert not memory_state.load_state()["project_checkpoint_pending"].get("demo")
    assert ProjectStore(adapter.ROOT, state_root).read_journal("demo")


def test_uncontended_prompt_still_commits_its_checkpoint(tmp_path, monkeypatch):
    state_root, project, _coordinator = _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *_args: False)
    envelope = adapter.normalize_occurrence_event("claude", "user_prompt", {
        "prompt": "continue", "cwd": str(project), "session_id": "free-gate",
        "event_id": "free-gate", "task_completed": True,
    })
    result = adapter.ingest_event(envelope)
    assert result["capture_durable"] is True
    assert not memory_state.load_state()["project_checkpoint_pending"].get("demo")
    assert ProjectStore(adapter.ROOT, state_root).read_journal("demo")
