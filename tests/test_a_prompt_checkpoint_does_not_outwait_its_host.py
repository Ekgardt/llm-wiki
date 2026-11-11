"""Durable hook capture must not wait another writer for its project handoff."""
import integration_adapter as adapter
import memory_state
import pytest
from llm_wiki import integration_capture_records, runtime_roots
from project_journal import ProjectStore

from tests import prompt_checkpoint_host_helpers as scenario

pytestmark = pytest.mark.shipped_append_budgets


@pytest.mark.parametrize("event", ["user_prompt", "post_tool_use"])
def test_capture_keeps_its_checkpoint_pending_without_waiting_past_host(tmp_path, monkeypatch, event):
    scenario.capture_keeps_its_checkpoint_pending_without_waiting_past_host(tmp_path, monkeypatch, event)


@pytest.mark.parametrize("event", ["user_prompt", "post_tool_use"])
def test_uncontended_worker_still_commits_the_captures_checkpoint(tmp_path, monkeypatch, event):
    state_root, project, _coordinator = scenario.configure(tmp_path, monkeypatch)
    monkeypatch.setattr(integration_capture_records, "wake_capture_worker", lambda *_args: False)
    envelope = adapter.normalize_occurrence_event("claude", event, {
        "prompt": "continue", "tool_name": "Bash",
        "tool_input": {"command": "pwd"}, "cwd": str(project), "session_id": "free-gate",
        "event_id": "free-gate", "task_completed": True,
    })
    result = adapter.ingest_event(envelope)
    assert result["capture_durable"] is True
    scenario.deliver(state_root)
    assert not memory_state.load_state().get("project_checkpoint_pending", {}).get("demo")
    assert ProjectStore(runtime_roots.vault_root(), state_root).read_journal("demo")
