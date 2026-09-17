"""A torn `run/state.json` need not be valid UTF-8, and a reader never gets a list.

See docs/research/2026-09-17-a-torn-state-file-is-recovered-whatever-its-bytes.md.
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

TORN = b'{"consolidated": tr\xff\xfe'


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


def test_bytes_that_are_not_utf8_are_recovered_from_the_previous_version(state_dir):
    memory_state.update_state(_mark("consolidated"))
    memory_state.update_state(_mark("compiled"))
    memory_state.STATE_FILE.write_bytes(TORN)

    memory_state.update_state(_mark("captured"))

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state == {"consolidated": True, "captured": True}
    assert (state_dir / "state.json.corrupt").read_bytes() == TORN


def test_a_reader_of_bytes_that_are_not_utf8_gets_an_empty_state(state_dir):
    state_dir.mkdir(parents=True)
    memory_state.STATE_FILE.write_bytes(TORN)

    assert memory_state.load_state() == {}
    assert (state_dir / "state.json.corrupt").read_bytes() == TORN


def test_a_reader_never_gets_anything_but_an_object(state_dir):
    state_dir.mkdir(parents=True)
    memory_state.STATE_FILE.write_text("[]", encoding="utf-8")

    assert memory_state.load_state() == {}
    assert (state_dir / "state.json.corrupt").read_text(encoding="utf-8") == "[]"
