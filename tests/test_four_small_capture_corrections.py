"""Bounded maps in the state file, and a stray byte that no longer loses a session.

See `docs/research/2026-09-17-four-small-capture-corrections.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import memory_state  # noqa: E402


@pytest.fixture
def own_state(tmp_path, monkeypatch):
    run = tmp_path / "run"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    return run


def test_the_prompt_counter_keeps_the_sessions_counted_most_recently(own_state):
    import prompt_capture_followup

    bound = prompt_capture_followup.MAX_PROMPT_COUNTERS
    sessions = [f"session-{index}" for index in range(bound + 1)]
    for session in [*sessions[:2], sessions[0], *sessions[2:]]:
        prompt_capture_followup.increment_prompt_count(session, "project")

    counters = memory_state.load_state()["user_prompt_counts"]
    kept = list(counters.items())
    assert (len(kept), kept[0], kept[-1]) == (bound, ("session-0", 2), (sessions[-1], 1))


def test_a_stray_byte_in_a_short_transcript_does_not_lose_the_session(tmp_path):
    import integration_adapter

    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b"we decided to keep \xff the queue\n")

    text = integration_adapter.capture_transcript_text(transcript)

    assert text == "we decided to keep � the queue\n"


def test_a_reducer_commit_preserves_entries_that_fit_the_state_byte_budget(own_state):
    import project_checkpoint
    from project_journal import CheckpointReducer

    reducers = {
        f"project-{index}:session": CheckpointReducer(observed_event_ids=(f"event-{index}",)).to_state()
        for index in range(128)
    }
    new = {"new-project:session": CheckpointReducer(observed_event_ids=("new-event",)).to_state()}
    expected = {**reducers, **new}
    item = {"event_id": "new-event", "claim_owner": "owner"}
    state = {"project_checkpoint_reducers": dict(reducers), "project_checkpoint_pending": {"queue": [item]}}
    project_checkpoint.commit_pending_state(state, "queue", "owner", [item], new)
    assert memory_state.state_bytes(state) < memory_state.MAX_STATE_TARGET_BYTES
    memory_state.save_state(state)
    restored = memory_state.load_state()
    assert restored["project_checkpoint_reducers"] == expected
    project_checkpoint.enqueue_pending_events(
        restored, "project-0:session", "project-0", [{"event_id": "event-0"}],
    )
    assert restored["project_checkpoint_pending"]["project-0"] == []
