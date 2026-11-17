"""Helpers shared by test_the_keys_have_one_reader_and_a_turn_is_not_asked_forever.py and the test files importing them."""
from __future__ import annotations

import json

DAILY = "knowledge/daily/2023-05-22.md"


BODY = (
    "# 2023-05-22\n\n## [10:00:00] session_end | s\n\n_captured: 2023-05-22 10:00:00_\n\n"
    "**user:** I sold my old drum set, a 5-piece Pearl Export.\n\n"
    "**assistant:** Noted.\n\n"
    "**user:** I keep a Korg piano, a Korg synth and a Korg tuner in the studio.\n\n"
    "**assistant:** Noted.\n"
)


def _covers_neither(prompt: str, system_prompt: str) -> str:
    return json.dumps({})


def _covers_both(prompt: str, system_prompt: str) -> str:
    return json.dumps({"0": ["I sold a Pearl Export drum set"], "1": ["I keep a Korg piano"]})
