"""`.previous` stays a separate copy of the version before the last change (audit 2026-09-27 C-20).

docs/research/2026-09-27-a-previous-state-is-a-different-file.md
"""
from __future__ import annotations

import json
import os

import memory_state

from tests.a_corrupt_state_is_not_replaced_by_an_empty_one_helpers import state_dir  # noqa: F401


def _set(key: str, value: object):
    def mutate(state: dict) -> None:
        state[key] = value

    return mutate


def test_an_unchanged_write_does_not_make_previous_the_same_file(state_dir) -> None:  # noqa: F811
    memory_state.update_state(_set("step", 1))
    memory_state.update_state(_set("step", 2))
    memory_state.update_state(_set("step", 2))
    previous = state_dir / "state.json.previous"

    kept = json.loads(previous.read_text(encoding="utf-8"))["step"]

    assert (os.path.samefile(state_dir / "state.json", previous), kept) == (False, 1)
