"""A flush appends to the daily log without holding the state lock, and still appends once.

The append ran inside `update_state`, so a busy Markdown writer made every hook's 0.1 s
state lock time out. Research:
`docs/research/2026-09-14-no-markdown-write-under-the-state-lock.md`.
"""
from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import flush_memory  # noqa: E402
import memory_state  # noqa: E402

ARGS = Namespace(session_id="s1", event="SessionEnd", source_event_id="evt-1")


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    monkeypatch.setattr(memory_state, "STATE_DIR", directory)
    monkeypatch.setattr(memory_state, "STATE_FILE", directory / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", directory / "state.json.lock")
    return directory


def test_the_append_runs_outside_the_lock_and_a_second_flush_skips(state_dir, monkeypatch):
    lock_held: list[bool] = []

    def append(day, block, operation_id=None):
        lock_held.append(memory_state.LOCK_FILE.exists())
        return state_dir / f"{day}.md"

    monkeypatch.setattr(flush_memory, "append_daily", append)

    first = flush_memory._persist_flush(ARGS, "major", "block", "2026-09-14")
    second = flush_memory._persist_flush(ARGS, "major", "block", "2026-09-14")

    assert (lock_held, len(first), second) == ([False], 1, [])


def test_a_failed_append_releases_its_claim(state_dir, monkeypatch):
    def refuse(day, block, operation_id=None):
        raise OSError("writer gate busy")

    monkeypatch.setattr(flush_memory, "append_daily", refuse)

    with pytest.raises(OSError):
        flush_memory._persist_flush(ARGS, "minor", "block", "2026-09-14")

    assert flush_memory.should_skip(memory_state.load_state(), ARGS.session_id, ARGS.event) is False
