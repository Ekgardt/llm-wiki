"""One reader of the JSON a model replies with, wherever in its reply it sits.

A fenced block, else the whole reply, else the last complete value written after
the model's notes. Of 34 LongMemEval replies refused as invalid JSON, 29 were a
prose reading followed by one complete, schema-valid document, bare. Taking the
value is not taking the model's word: every caller still validates what it got.
See `docs/research/2026-09-14-the-document-after-the-notes.md` and
`docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

_FENCED_JSON_RE = re.compile(r"```[^\n]*\n(?P<body>.*?)\n?\s*```", re.DOTALL)
_JSON_DECODER = json.JSONDecoder()


def unfenced(raw: str) -> str:
    """The first fenced block in the reply, or the text unchanged.

    Providers answer a "reply with JSON" instruction either bare or wrapped in a
    ```json fence, and which one they pick varies with the answer; a sentence of
    commentary before or after the fence is common (15 of 200 replies measured on
    2026-09-02). The prose around it is discarded, never shown.
    """
    match = _FENCED_JSON_RE.search(raw)
    if not match:
        return raw
    return match.group("body")


def _value_at(text: str, start: int) -> tuple[object, int] | None:
    try:
        return _JSON_DECODER.raw_decode(text, start)
    except json.JSONDecodeError:
        return None


def _last_value_in(text: str, opener: str, accept: Callable[[object], bool]) -> object | None:
    """The last acceptable JSON value that starts with `opener`, or None."""
    found = None
    start = text.find(opener)
    while start != -1:
        decoded = _value_at(text, start)
        if decoded is None:
            start = text.find(opener, start + 1)
            continue
        found = decoded[0] if accept(decoded[0]) else found
        start = text.find(opener, decoded[1])
    return found


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


def _is_object_array(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _reply_value(raw: str, opener: str, accept: Callable[[object], bool]) -> object:
    try:
        return json.loads(unfenced(raw))
    except json.JSONDecodeError as refused:
        found = _last_value_in(raw, opener, accept)
        if found is None:
            raise refused
        return found


def reply_document(raw: str) -> object:
    """The JSON document (an object) a provider replied with."""
    return _reply_value(raw, "{", _is_object)


def reply_array(raw: str) -> object:
    """The JSON array of objects a provider replied with; a bracketed note is not one."""
    return _reply_value(raw, "[", _is_object_array)
