"""A reply is read as one answer of the expected shape, or refused as unreadable.

Taking the last JSON value let a quoted verdict decide a contradiction check and an
example array replace a day's lessons. Research:
`docs/research/2026-09-14-one-answer-or-none.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import reply_json  # noqa: E402

VERDICT = {"label": "compatible", "confidence": "high", "supported": True}
QUOTED = {"label": "contradiction", "confidence": "high", "supported": True}


def test_a_verdict_followed_by_a_quoted_verdict_is_refused_not_flipped():
    reply = json.dumps(VERDICT) + "\nThe claim itself said: " + json.dumps(QUOTED)

    with pytest.raises(ValueError):
        reply_json.reply_document(reply, reply_json.object_with("label"))


def test_lessons_followed_by_an_example_array_are_refused_so_the_day_is_asked_again():
    lessons = [{"kind": "lesson", "text": "t", "quote": "q", "session": "s"}]
    reply = json.dumps(lessons) + "\nExample format: " + json.dumps([{"lesson": "..."}])

    with pytest.raises(ValueError):
        reply_json.reply_array(reply)


def test_deeply_nested_unclosed_json_is_unreadable_not_a_crash():
    with pytest.raises(ValueError):
        reply_json.reply_document("notes " + "[" * 100000 + "{")


def test_the_one_answer_among_other_shapes_is_still_found():
    reply = 'See {"size": 3}.\n' + json.dumps(VERDICT)

    assert reply_json.reply_document(reply, reply_json.object_with("label")) == VERDICT
