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

from tests.a_corrupt_state_is_not_replaced_by_an_empty_one_helpers import state_dir  # noqa: E402, F401


def _mark(key: str):
    def mutate(state: dict) -> None:
        state[key] = True

    return mutate


def test_a_corrupt_file_is_recovered_from_its_previous_version(state_dir):  # noqa: F811
    memory_state.update_state(_mark("consolidated"))
    memory_state.update_state(_mark("compiled"))
    memory_state.state_file().write_text("{ torn", encoding="utf-8")

    memory_state.update_state(_mark("captured"))
    state = json.loads(memory_state.state_file().read_text(encoding="utf-8"))

    assert state == {"consolidated": True, "captured": True}


def test_with_no_readable_version_nothing_is_written(state_dir):  # noqa: F811
    state_dir.mkdir(parents=True)
    memory_state.state_file().write_text("{ torn", encoding="utf-8")

    with pytest.raises(memory_state.StateCorrupt):
        memory_state.update_state(_mark("captured"))

    assert memory_state.state_file().read_text(encoding="utf-8") == "{ torn"
