"""One reader of the JSON a model replies with: one answer of the expected shape, or none.

A fenced block or the whole reply when it parses to the shape the caller expects;
otherwise every complete value of that shape written in the reply — exactly one is
the answer, none or several is unreadable. Of 34 LongMemEval replies refused as
invalid JSON, 29 held one schema-valid document after prose notes; taking "the last
value" instead let a quoted verdict decide a contradiction check and an example array
replace a day's lessons. Every caller still validates what it gets. See
`docs/research/2026-09-14-the-document-after-the-notes.md` and
`docs/research/2026-09-14-one-answer-or-none.md`.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

_FENCED_JSON_RE = re.compile(r"```[^\n]*\n(?P<body>.*?)\n?\s*```", re.DOTALL)
_JSON_DECODER = json.JSONDecoder()
Shape = Callable[[object], bool]


def unfenced(raw: str) -> str:
    """The first fenced block in the reply, or the text unchanged."""
    match = _FENCED_JSON_RE.search(raw)
    if not match:
        return raw
    return match.group("body")


def is_object(value: object) -> bool:
    return isinstance(value, dict)


def is_object_array(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def object_with(key: str) -> Shape:
    """The shape of an object that carries `key`."""

    def shaped(value: object) -> bool:
        return isinstance(value, dict) and key in value

    return shaped


def _value_at(text: str, start: int) -> tuple[object, int] | None:
    try:
        return _JSON_DECODER.raw_decode(text, start)
    except (json.JSONDecodeError, RecursionError):
        return None


def _shaped_values_in(text: str, opener: str, shape: Shape) -> list[object]:
    """Every complete top-level value of `shape` that starts with `opener`."""
    found: list[object] = []
    start = text.find(opener)
    while start != -1:
        decoded = _value_at(text, start)
        if decoded is None:
            start = text.find(opener, start + 1)
            continue
        found.extend([decoded[0]] if shape(decoded[0]) else [])
        start = text.find(opener, decoded[1])
    return found


def _parsed(text: str) -> tuple[object] | None:
    try:
        return (json.loads(text),)
    except (json.JSONDecodeError, RecursionError):
        return None


def _whole(raw: str, shape: Shape) -> object | None:
    """The fenced block, or else the whole reply, when it is exactly one value of `shape`."""
    parsed = _parsed(unfenced(raw))
    if parsed is None or not shape(parsed[0]):
        return None
    return parsed[0]


def _reply_value(raw: str, opener: str, shape: Shape) -> object:
    whole = _whole(raw, shape)
    if whole is not None:
        return whole
    found = _shaped_values_in(raw, opener, shape)
    if len(found) != 1:
        raise json.JSONDecodeError(f"{len(found)} JSON values of the expected shape", raw, 0)
    return found[0]


def reply_document(raw: str, shape: Shape = is_object) -> object:
    """The one JSON object of `shape` a provider replied with."""
    return _reply_value(raw, "{", shape)


def reply_array(raw: str, shape: Shape = is_object_array) -> object:
    """The one JSON array of `shape` a provider replied with."""
    return _reply_value(raw, "[", shape)
