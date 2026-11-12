"""Helpers shared by test_structure.py and the test files importing them."""
from __future__ import annotations

import re


def _linked_note_paths(text: str) -> set[str]:
    """The note pages this file names by path: a wikilink, or a back-quoted path."""
    linked = re.findall(r"\[\[knowledge/notes/([^\]|]+)", text)
    quoted = re.findall(r"`knowledge/notes/([^`]+?)\.md`", text)
    return {f"knowledge/notes/{name}.md" for name in [*linked, *quoted]}
