"""Helpers shared by test_pruning.py and the test files importing them."""
from __future__ import annotations

import numpy as np

REPLY = (
    "**assistant:** Sure, here are some thoughts. Drums need fresh heads every year. "
    "A Pearl Export is a classic kit. Guitars like a humidifier. "
    "Pianos should be tuned twice a year. Let me know if you want more. "
    + "There is a great deal more one could say about instrument care in general. " * 12
    + "\n"
)


FILLER = "## [09:00:00] session_end | earlier\n\n**user:** " + "We talked about the weather for a while. " * 60 + "\n\n"


CONTENT = (
    "# Day\n\n" + FILLER + "## [10:00:00] session_end | s\n\n**user:** How do I care for my instruments?\n\n" + REPLY
).encode()


def _about_drums(text: str) -> float:
    lowered = text.casefold()
    return float("drum" in lowered or "pearl" in lowered)


def _encode(texts, is_query):
    """A fake dual encoder: a sentence about drums points the way the question does."""
    if is_query:
        return np.asarray([[1.0, 1.0] for _ in texts], dtype=float)
    return np.asarray([[_about_drums(text), 1.0] for text in texts], dtype=float)
