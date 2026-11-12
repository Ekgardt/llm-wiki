"""Recognise fenced code blocks in the bytes of a Markdown page."""
from __future__ import annotations

import re

_FENCE = re.compile(rb"^[ ]{0,3}(`{3,}|~{3,})([^\r\n]*)$")


def closing_fence(body: bytes, character: bytes, length: int) -> bool:
    pattern = (
        rb"[ ]{0,3}"
        + re.escape(character)
        + rb"{" + str(length).encode("ascii") + rb",}[ \t]*"
    )
    return re.fullmatch(pattern, body) is not None


def opening_fence(body: bytes):
    """The fence that opens a block, or None — a backtick in the info string is not one."""
    opening = _FENCE.fullmatch(body)
    if opening is None:
        return None
    if opening.group(1).startswith(b"`") and b"`" in opening.group(2):
        return None
    return opening
