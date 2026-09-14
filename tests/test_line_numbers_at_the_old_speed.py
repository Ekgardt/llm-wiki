"""A source's line numbers are found once per file, not once per node.

A per-node count from byte zero made a 314 KB file 100 times slower. Research:
`docs/research/2026-09-14-line-numbers-at-the-old-speed.md`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import code_extractor  # noqa: E402

SOURCE = "class A:\n    def f(self):\n        return g(1)\n\n\ndef g(x):\n    return x\n"


def _spans(content: bytes) -> list[tuple[int, int, int, int]]:
    offsets = code_extractor._line_offsets(content)
    nodes = [node for node in ast.walk(ast.parse(content)) if hasattr(node, "lineno")]
    return [code_extractor._span(node, offsets, content) for node in nodes]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_lines_are_the_writers_count_of_line_feeds(ending):
    content = SOURCE.encode().replace(b"\n", ending)

    expected = [
        (start, end, content.count(b"\n", 0, start) + 1, content.count(b"\n", 0, end) + 1)
        for start, end, _line, _end_line in _spans(content)
    ]

    assert _spans(content) == expected


def test_one_file_is_scanned_for_line_feeds_once():
    content = (SOURCE * 200).encode()
    code_extractor._newline_positions.cache_clear()

    _spans(content)

    assert code_extractor._newline_positions.cache_info().misses == 1
