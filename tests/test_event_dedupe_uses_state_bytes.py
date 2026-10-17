"""Observed event identities share the state byte budget, without count eviction."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import integration_adapter
import memory_state
from project_journal import CheckpointReducer

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _observed_reducer(count):
    reducer = CheckpointReducer(host_progress_signals=True)
    ids = []
    for index in range(count):
        event_id = hashlib.sha256(str(index).encode()).hexdigest()
        ids.append(event_id)
        reducer.observe({"type": "read", "event_id": event_id}, now=AT + timedelta(seconds=index))
    return reducer, ids


def test_a_retained_event_is_not_requeued_after_256_later_observations(monkeypatch, tmp_path):
    reducer, ids = _observed_reducer(257)
    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "state.json")
    state = {"project_checkpoint_reducers": {"demo:session": reducer.to_state()}}
    memory_state.save_state(state)
    restored = memory_state.load_state()
    integration_adapter._enqueue_pending_events(restored, "demo:session", "demo", [{"event_id": ids[0]}])
    assert restored["project_checkpoint_pending"]["demo"] == []
    assert restored["project_checkpoint_reducers"]["demo:session"]["observed_event_ids"] == ids
    assert (tmp_path / "state.json").stat().st_size < memory_state.MAX_STATE_TARGET_BYTES


def test_actual_byte_pressure_still_evicts_a_disposable_reducer_cache():
    reducer = CheckpointReducer(observed_event_ids=["x" * memory_state.MAX_STATE_TARGET_BYTES])
    state = {"project_checkpoint_reducers": {"demo:session": reducer.to_state()}, "project_checkpoint_pending": {"demo": [{"event_id": "retained-pending"}]}}
    assert memory_state.trim_state_to_budget(state) == 1
    assert state["project_checkpoint_reducers"] == {}
    assert state["project_checkpoint_pending"] == {"demo": [{"event_id": "retained-pending"}]}
    assert len(json.dumps(state).encode()) < memory_state.MAX_STATE_TARGET_BYTES
