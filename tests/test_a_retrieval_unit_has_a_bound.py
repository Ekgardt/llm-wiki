"""A retrieval unit larger than the answer budget can only ever arrive alone.

Measured on this vault: a captured session becomes one heading span of about
10 KB, roughly 2 500 tokens, against a 28 672-byte answer budget — so a median
of two units of twelve retrieved reach the answer model. Retrieval ranks the
right session first and the compiler places everything it is given; the unit is
what narrows it. By judge accuracy over three runs of 200, the categories that
need facts from more than one session score 0.0216 and 0.0988.

2026 benchmarking puts the useful range for analytical and multi-hop queries at
512 to 1 024 tokens. Ours was two and a half times the top of it. Turn-level was
the other candidate and the sources argue against it — too fine is fragmentary,
and session-level beats turn-level on its own — so the cut is at a paragraph
boundary, which in a rendered transcript falls between speaker turns and between
topics inside a long one.

See `docs/research/2026-09-02-the-unit-of-retrieval.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import corpus_snapshot  # noqa: E402

BOUND = corpus_snapshot.MAX_SPAN_BYTES


def _paragraphs(count: int, size: int = 500) -> bytes:
    return b"\n\n".join(b"p%d " % index + b"x" * size for index in range(count))


def _spans(content: bytes) -> tuple:
    return corpus_snapshot._retrieval_spans(
        content, 0, heading_enabled=True, deadline=None, cancelled=None
    )


def test_a_long_section_becomes_several_units() -> None:
    """The whole point: one session must stop being one indivisible block."""
    content = b"# Session\n\n" + _paragraphs(30)

    spans = _spans(content)

    assert len(spans) > 1


def test_no_unit_exceeds_the_bound_by_more_than_one_paragraph() -> None:
    content = b"# Session\n\n" + _paragraphs(30)

    for start, end, _ancestry in _spans(content):
        assert end - start <= BOUND + 600


def test_a_short_section_is_left_whole() -> None:
    """Splitting what already fits would only fragment it."""
    content = b"# Short\n\nonly a little text here\n"

    assert len(_spans(content)) == 1


def test_every_piece_keeps_the_heading_it_came_from() -> None:
    """Parent context is what stops a small chunk from being fragmentary."""
    content = b"# Tokyo trip\n\n" + _paragraphs(30)

    ancestries = {ancestry for _s, _e, ancestry in _spans(content)}

    assert ancestries == {("Tokyo trip",)}


def test_the_pieces_tile_the_section_without_gap_or_overlap() -> None:
    """A dropped byte is a lost fact; a repeated one is a duplicated citation."""
    content = b"# Session\n\n" + _paragraphs(30)

    spans = sorted(_spans(content))
    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] == later[0]
    assert spans[-1][1] == len(content)


def test_a_cut_never_lands_inside_a_line() -> None:
    content = b"# Session\n\n" + _paragraphs(30)

    for start, _end, _ancestry in _spans(content):
        assert start == 0 or content[start - 1:start] == b"\n"


def test_a_section_with_no_break_at_all_still_terminates() -> None:
    """One paragraph longer than the bound must not loop or vanish."""
    content = b"# Session\n\n" + b"y" * (BOUND * 3)

    spans = _spans(content)

    assert spans
    assert sum(end - start for start, end, _a in spans) > BOUND * 2


def test_two_sections_stay_two_ancestries() -> None:
    content = b"# One\n\n" + _paragraphs(12) + b"\n\n# Two\n\n" + _paragraphs(12)

    ancestries = {ancestry for _s, _e, ancestry in _spans(content)}

    assert ancestries == {("One",), ("Two",)}


def test_the_bound_sits_in_the_band_the_sources_report() -> None:
    """512–1 024 tokens; four bytes to the token is the estimate used elsewhere."""
    assert 512 * 4 <= BOUND <= 1024 * 4
