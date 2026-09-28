"""An entry longer than a part is cut inside itself, so a busy day always compiles.

On 2026-09-27 one transactional append carried a 49 964-byte pre-compact summary.
The day was split only between entries, that part exceeded the compile budget, and
the refusal stopped every day's compile. See
`docs/research/2026-09-28-a-long-entry-is-cut-inside-itself.md`.
"""

from __future__ import annotations

import compile_memory
from evidence_resolver import MAX_DAILY_PART_BYTES, _daily_part_bounds

LOGICAL = "knowledge/daily/2026-09-27.md"


def _entry(index: int, body: bytes) -> bytes:
    return f"<!-- llm-wiki-operation:{index:064x} -->\n".encode() + body


def _long_block(lines: int) -> bytes:
    return b"\n## [12:26:31] pre-compact | session\n" + b"".join(
        f"- line {n} of a long summary, kept whole\n".encode() for n in range(lines)
    )


def _pieces(content: bytes) -> list[bytes]:
    return [content[start:end] for start, end in _daily_part_bounds(content)]


def test_a_long_entry_leaves_no_part_longer_than_a_part_and_loses_nothing() -> None:
    content = _entry(1, b"- short\n") + _entry(2, _long_block(2000)) + _entry(3, b"- after\n")
    pieces = _pieces(content)
    assert (max(map(len, pieces)) <= MAX_DAILY_PART_BYTES, b"".join(pieces) == content) == (True, True)


def test_a_day_whose_entries_fit_is_cut_between_entries_as_before() -> None:
    entries = [_entry(index, b"x" * 10_000 + b"\n") for index in range(4)]
    content = b"".join(entries)
    starts = [sum(map(len, entries[:index])) for index in range(1, 4)]
    assert [start for start, _end in _daily_part_bounds(content)][1:] == starts


def test_a_line_without_breaks_is_cut_on_a_character_boundary() -> None:
    content = _entry(1, "ж".encode() * 30_000)
    for piece in _pieces(content):
        piece.decode("utf-8")


def test_a_day_with_a_long_entry_packs_into_batches() -> None:
    content = _entry(1, _long_block(2000))
    inputs = compile_memory.CompileInputs(
        dailies=tuple(compile_memory._daily_parts(LOGICAL, content)), sources=(), targets=()
    )
    assert len(compile_memory.pack_compile_batches(inputs, model=None)) >= 2
