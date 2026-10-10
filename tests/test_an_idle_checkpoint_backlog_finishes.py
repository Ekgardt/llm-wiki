"""Unattended checkpoint drain advances a quiet project's elapsed debounce."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import integration_adapter as adapter
import memory_state
from project_journal import CheckpointReducer, ProjectStore

AT = datetime(2026, 9, 29, 10, 3, 34, tzinfo=timezone.utc)


class _MaintenanceClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return (AT + timedelta(days=1)).astimezone(tz)


def _event(index):
    payload = {"session_id": "idle-session", "tool_name": "Edit", "tool_input": {"file_path": f"scripts/example_{index}.py"}, "changed": True}
    return adapter.normalize_event("codex", "post_tool_use", payload, occurred_at=AT + timedelta(seconds=index + 1))


def _idle_state():
    key = "demo:idle-session"
    reducer = CheckpointReducer(host_progress_signals=True, last_checkpoint_at=AT)
    return {"project_checkpoint_pending": {"demo": [adapter._pending_checkpoint(_event(index), "demo", key) for index in range(8)]}, "project_checkpoint_reducers": {key: reducer.to_state()}}


def _configure(monkeypatch, tmp_path, state):
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    monkeypatch.setattr(adapter, "datetime", _MaintenanceClock)
    monkeypatch.setattr(adapter, "ROOT", vault)
    monkeypatch.setattr(adapter, "STATE_ROOT", runtime)
    monkeypatch.setattr(memory_state, "load_state", lambda: state)
    def update(mutator, **kwargs):
        mutator(state)
        return state
    monkeypatch.setattr(adapter, "update_state", update)
    return vault, runtime


def test_unattended_drain_commits_an_idle_delta_without_another_event(monkeypatch, tmp_path):
    state = _idle_state()
    vault, runtime = _configure(monkeypatch, tmp_path, state)
    result = adapter.drain_pending_backlog()
    assert result == {"drained": {"demo": 8}, "failed": {}, "remaining": []}
    journal = ProjectStore(vault, runtime).read_journal("demo")
    assert all(f"scripts/example_{index}.py" in journal for index in range(8))
    persisted = state["project_checkpoint_reducers"]["demo:idle-session"]["last_checkpoint_at"]
    assert datetime.fromisoformat(persisted.replace("Z", "+00:00")) == AT + timedelta(seconds=8)
