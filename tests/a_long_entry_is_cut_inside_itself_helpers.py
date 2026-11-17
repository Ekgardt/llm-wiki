"""Helpers shared by test_a_long_entry_is_cut_inside_itself.py and the test files importing them."""
from __future__ import annotations

LOGICAL = "knowledge/daily/2026-09-27.md"


def _entry(index: int, body: bytes) -> bytes:
    return f"<!-- llm-wiki-operation:{index:064x} -->\n".encode() + body
