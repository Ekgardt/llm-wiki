"""A writer never replaces an unreadable `run/state.json` with an almost empty one.

`update_state` read `{}` from a corrupt file and saved it with one change, losing the
consolidated days, the compile timestamps and the capture checkpoints. Research:
`docs/research/2026-09-14-a-corrupt-state-is-not-replaced-by-an-empty-one.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_state  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    monkeypatch.setattr(memory_state, "STATE_DIR", directory)
    monkeypatch.setattr(memory_state, "STATE_FILE", directory / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", directory / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    return directory


def _mark(key: str):
    def mutate(state: dict) -> None:
        state[key] = True

    return mutate


def test_a_corrupt_file_is_recovered_from_its_previous_version(state_dir):
    memory_state.update_state(_mark("consolidated"))
    memory_state.update_state(_mark("compiled"))
    memory_state.STATE_FILE.write_text("{ torn", encoding="utf-8")

    memory_state.update_state(_mark("captured"))
    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))

    assert state == {"consolidated": True, "captured": True}


def test_with_no_readable_version_nothing_is_written(state_dir):
    state_dir.mkdir(parents=True)
    memory_state.STATE_FILE.write_text("{ torn", encoding="utf-8")

    with pytest.raises(memory_state.StateCorrupt):
        memory_state.update_state(_mark("captured"))

    assert memory_state.STATE_FILE.read_text(encoding="utf-8") == "{ torn"
