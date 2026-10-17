"""Foreground capture does not perform the recoverable project transaction."""
from functools import partial

import flush_memory
import integration_adapter as adapter
import markdown_transaction
import memory_queue
import memory_state
import pytest
from project_journal import ProjectStore

from tests.adopted_capture_vault import adopted_capture_vault
from tests.test_capture_terminal import _expire_capture_lease


@pytest.fixture
def captured(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(memory_state, "STATE_FILE", state / "run/state.json")
    monkeypatch.setattr(adapter, "_project_context", lambda event: ("demo", project))
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *_args: False)
    envelope = adapter.normalize_occurrence_event("claude", "user_prompt", {
        "prompt": "Keep this project evidence", "cwd": str(project),
        "session_id": "project-worker", "event_id": "project-worker", "task_completed": True,
    })
    queue = memory_queue.active_memory_queue(adapter.ROOT, state)
    coordinator = markdown_transaction.active_markdown_coordinator(adapter.ROOT, state)
    work = partial(flush_memory.run_capture_worker_once, queue, coordinator,
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator), handler_versions=(2,))
    return envelope, state, queue, work


def test_prompt_foreground_never_runs_the_project_transaction(captured, monkeypatch):
    envelope, _state, _queue, _work = captured
    def forbidden(*args, **kwargs):
        pytest.fail("foreground hook performed project checkpoint")
    monkeypatch.setattr(adapter, "_observe_project_checkpoint", forbidden)
    result = adapter.ingest_event(envelope)
    assert result["capture_durable"]
    assert "post_capture_error" not in result


def test_worker_commits_project_checkpoint_before_terminal_completion(captured):
    envelope, state, queue, work = captured
    result = adapter.ingest_event(envelope)
    journal = adapter.ROOT / "knowledge/projects/demo/journal.md"
    assert not journal.exists()
    assert work()
    assert ProjectStore(adapter.ROOT, state).read_journal("demo")
    assert (state / f"run/queue-results/capture-{result['capture_intent_ids'][0]}.json").exists()
    assert work() is None


def test_failed_project_checkpoint_retains_capture_for_real_worker_retry(captured, monkeypatch):
    envelope, state, queue, work = captured
    result = adapter.ingest_event(envelope)
    original = adapter._observe_project_checkpoint
    def interrupted(*args, **kwargs):
        raise OSError("project write interrupted")
    monkeypatch.setattr(adapter, "_observe_project_checkpoint", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        work()
    assert not (state / f"run/queue-results/capture-{result['capture_intent_ids'][0]}.json").exists()
    monkeypatch.setattr(adapter, "_observe_project_checkpoint", original)
    with queue.connection() as database:
        task_id = database.execute("SELECT id FROM tasks").fetchone()[0]
    _expire_capture_lease(queue, task_id)
    assert work()
    assert ProjectStore(adapter.ROOT, state).read_journal("demo")


@pytest.mark.parametrize("timestamp", [None, "2026-10-01T12:00:00Z"])
def test_worker_reconstructs_the_original_native_identity_and_times(captured, timestamp):
    import breadcrumb_storage
    import breadcrumb_worker

    envelope, state, _queue, _work = captured
    raw = {"prompt": "same occurrence", "cwd": envelope.worktree,
           "session_id": envelope.session, "event_id": "identity-proof", "timestamp": timestamp}
    original = adapter.normalize_occurrence_event("claude", "user_prompt", raw)
    result = adapter.ingest_event(original)
    identity = result["capture_intent_ids"][0]
    bundle = breadcrumb_storage.load_bundle(state, breadcrumb_storage._stored_manifest(state, identity))
    restored = breadcrumb_worker._checkpoint_envelope(bundle)
    assert restored.to_dict() == original.to_dict()


@pytest.mark.parametrize("damage", [{"schema_version": "unknown"}, {"event_type": "stop"}])
def test_invalid_claimed_native_event_cannot_complete_a_capture(captured, damage):
    import breadcrumb_storage

    envelope, state, queue, work = captured
    coordinator = markdown_transaction.active_markdown_coordinator(adapter.ROOT, state)
    event = adapter._breadcrumb_input(envelope)
    event.update(damage)
    publication = breadcrumb_storage.publish_breadcrumb(queue, coordinator,
        {"host": "claude", "session": "invalid-native", "kind": "user_prompt", "occurrence": "invalid-native"},
        event, occurred_at=envelope.occurred_at, accepted_at=envelope.captured_at, time_origin="acceptance")
    with pytest.raises(ValueError, match="native checkpoint"):
        work()
    assert not (state / f"run/queue-results/capture-{publication.intent_id}.json").exists()
    bundle = breadcrumb_storage.load_bundle(state, breadcrumb_storage._stored_manifest(state, publication.intent_id))
    assert bundle.content



def test_project_commit_retry_preserves_one_journal_checkpoint(captured, monkeypatch):
    envelope, state, queue, work = captured
    result = adapter.ingest_event(envelope)
    original = adapter._observe_project_checkpoint
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("after project commit")
    monkeypatch.setattr(adapter, "_observe_project_checkpoint", interrupted)
    with pytest.raises(OSError, match="after project commit"):
        work()
    prior = ProjectStore(adapter.ROOT, state).read_journal("demo")
    assert prior
    assert not (state / f"run/queue-results/capture-{result['capture_intent_ids'][0]}.json").exists()
    monkeypatch.setattr(adapter, "_observe_project_checkpoint", original)
    with queue.connection() as database:
        task_id = database.execute("SELECT id FROM tasks").fetchone()[0]
    _expire_capture_lease(queue, task_id)
    assert work()
    assert ProjectStore(adapter.ROOT, state).read_journal("demo") == prior
