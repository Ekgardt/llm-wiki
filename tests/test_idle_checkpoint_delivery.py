"""A persisted debounce buffer drains without a later lifecycle event."""

import json
from datetime import datetime, timedelta, timezone

import integration_adapter as adapter
import memory_state
import pytest
from project_journal import CheckpointReducer

from tests.adopted_capture_vault import adopted_capture_vault


@pytest.fixture
def idle_project(tmp_path, monkeypatch):
    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_root / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run/state.json")
    monkeypatch.setattr(memory_state, "ROOT", adapter.ROOT)
    monkeypatch.setattr(adapter, "_project_context", lambda event: ("demo", project))
    start = datetime(2026, 9, 28, 14, 39, 38, tzinfo=timezone.utc)
    state = {"project_checkpoint_reducers": {
        "demo:s1": CheckpointReducer(last_checkpoint_at=start).to_state()
    }}
    memory_state.update_state(lambda target: target.update(state))
    event = adapter.normalize_event("claude", "post_tool_use", {
        "session_id": "s1", "cwd": str(project), "event_id": "saved-tool",
        "tool_name": "Bash", "tool_input": {"command": "git status --short"},
    }, occurred_at=start + timedelta(seconds=29))
    adapter._observe_project_checkpoint(event)
    assert adapter._pending_backlog_depth("demo") == 1
    return start, event, adapter.ROOT, state_root


def _clock(monkeypatch, instant):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(adapter, "datetime", Clock)


@pytest.mark.parametrize("elapsed", [30, 2 * 24 * 60 * 60])
def test_maintenance_drains_an_idle_saved_queue_once(idle_project, monkeypatch, elapsed):
    start, event, root, state_root = idle_project
    _clock(monkeypatch, start + timedelta(seconds=29))
    assert adapter.drain_pending_backlog()["drained"] == {"demo": 0}
    saved = json.loads((state_root / "run/state.json").read_text())
    assert saved["project_checkpoint_pending"]["demo"][0]["event_id"] == event.event_id
    _clock(monkeypatch, start + timedelta(seconds=elapsed))
    report = adapter.drain_pending_backlog()
    assert report == {"drained": {"demo": 1}, "failed": {}, "remaining": []}
    journal = root / "knowledge/projects/demo/journal.md"
    original = journal.read_bytes()
    assert b"git status --short" in original
    assert event.event_id.encode() in original
    reducer = CheckpointReducer.from_state(memory_state.load_state()["project_checkpoint_reducers"]["demo:s1"])
    assert reducer.last_checkpoint_at == start + timedelta(seconds=29)
    assert adapter.drain_pending_backlog() == {"drained": {}, "failed": {}, "remaining": []}
    assert journal.read_bytes() == original


def test_a_committed_checkpoint_replays_its_saved_batch_after_state_failure(idle_project, monkeypatch):
    start, _, root, _ = idle_project
    _clock(monkeypatch, start + timedelta(seconds=30))
    commit = adapter._commit_pending

    def unavailable(*args, **kwargs):
        raise OSError("state publication failed after journal commit")

    monkeypatch.setattr(adapter, "_commit_pending", unavailable)
    report = adapter.drain_pending_backlog()
    assert "state publication failed" in report["failed"]["demo"]
    assert adapter._pending_backlog_depth("demo") == 1
    journal = root / "knowledge/projects/demo/journal.md"
    original = journal.read_bytes()
    monkeypatch.setattr(adapter, "_commit_pending", commit)
    _clock(monkeypatch, start + timedelta(days=2))
    assert adapter.drain_pending_backlog() == {"drained": {"demo": 1}, "failed": {}, "remaining": []}
    assert journal.read_bytes() == original
