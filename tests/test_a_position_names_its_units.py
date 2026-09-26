"""A position past the line end is the line end; the schema names its units (audit 2026-09-26 C-9).

docs/research/2026-09-26-a-position-names-its-units.md
"""
from __future__ import annotations

import pytest
from lsp_positions import SourceDocument

_POSITION_KEYS = frozenset({"line", "character", "column"})


def test_a_character_past_the_line_end_is_the_line_end() -> None:
    document = SourceDocument.from_bytes("a.py", b"ab\ncd\n")

    anchor = document.validate_anchor(line=1, character=99)

    assert (anchor.utf8_character, anchor.byte_offset) == (2, 2)


def test_a_character_inside_a_code_point_is_still_refused() -> None:
    document = SourceDocument.from_bytes("a.py", "é = 1\n".encode())

    with pytest.raises(UnicodeDecodeError):
        document.validate_anchor(line=1, character=1)


def _undocumented_positions(schema: dict) -> list[str]:
    properties = schema.get("properties", {})
    return [name for name in sorted(_POSITION_KEYS & set(properties)) if "-based" not in properties[name].get("description", "")]


def test_every_position_field_of_every_tool_names_its_base() -> None:
    from mcp_server import TOOL_INPUT_SCHEMAS

    undocumented = {tool: _undocumented_positions(schema) for tool, schema in TOOL_INPUT_SCHEMAS.items()}

    assert {tool: names for tool, names in undocumented.items() if names} == {}
