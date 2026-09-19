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
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    return run


def test_the_prompt_counter_keeps_the_sessions_counted_most_recently(own_state):
    import user_prompt_capture

    bound = user_prompt_capture.MAX_PROMPT_COUNTERS
    sessions = [f"session-{index}" for index in range(bound + 1)]
    for session in [*sessions[:2], sessions[0], *sessions[2:]]:
        user_prompt_capture._increment_prompt_count(session, "project")

    counters = memory_state.load_state()["user_prompt_counts"]
    kept = list(counters.items())
    assert (len(kept), kept[0], kept[-1]) == (bound, ("session-0", 2), (sessions[-1], 1))


def test_a_commit_that_adds_several_reducers_is_trimmed_back_to_the_bound():
    import integration_adapter

    bound = integration_adapter.MAX_CHECKPOINT_REDUCERS
    reducers = {f"project-{index}": {} for index in range(bound + 5)}

    integration_adapter._trim_reducers(reducers)

    assert (len(reducers), next(iter(reducers))) == (bound, "project-5")


def test_a_stray_byte_in_a_short_transcript_does_not_lose_the_session(tmp_path):
    import integration_adapter

    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b"we decided to keep \xff the queue\n")

    text = integration_adapter._capture_transcript_text(transcript)

    assert text == "we decided to keep � the queue\n"
