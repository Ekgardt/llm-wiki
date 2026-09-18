"""Building the context obeys the answer's deadline, says what it shed, and prunes the first turn.

Third audit, 2026-09-17, lows L8 to L11, worked on 2026-09-18. The shedding loop recompiled
after every dropped span with no deadline to stop it; the rendering dropped evidence off the
tail and told nobody; four best-effort failures were swallowed in silence; and the first turn
of an entry was never pruned because the chunk starts with the entry's heading.
See `docs/research/2026-09-18-the-context-is-built-under-the-same-clock.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_pruning  # noqa: E402
import query_memory  # noqa: E402

HEADING = b"## [10:00:00] session_end | s\n\n_captured: 2023-05-22 10:00:00_\n\n"
LONG_TURN = b"**user:** " + b"I bought the amplifier in Berlin. " * 40 + b"It still works.\n"


def _chunk(page: str, identity: str, position: int):
    """One chunk of a page, of the shape `_chunks_by_page` and the compiler read."""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=identity,
        parent_page=page,
        byte_start=position,
        byte_end=position + 10,
        text="**user:** something",
        type="daily-evidence",
    )


def _snapshot(chunks):
    from types import SimpleNamespace

    return SimpleNamespace(chunks=tuple(chunks), sources=())


def test_the_narrowed_snapshot_keeps_the_corpus_order_through_the_index() -> None:
    chunks = [_chunk("a.md", "a1", 0), _chunk("b.md", "b1", 10), _chunk("a.md", "a2", 20)]
    snapshot = _snapshot(chunks)

    through_index = query_memory._chunks_of(snapshot, ("a.md", "b.md"), query_memory._chunks_by_page(snapshot))
    without_index = query_memory._chunks_of(snapshot, ("a.md", "b.md"), None)

    assert [chunk.id for chunk in through_index] == ["a1", "b1", "a2"]
    assert [chunk.id for chunk in without_index] == ["a1", "b1", "a2"]


def test_a_passed_deadline_stops_the_compilation_instead_of_shedding_on() -> None:
    snapshot = _snapshot([_chunk("a.md", "a1", 0)])

    with pytest.raises(TimeoutError):
        query_memory._compiled_for(snapshot, (), object(), None, time.monotonic() - 1.0)


def test_without_a_deadline_the_compilation_is_not_timed_out() -> None:
    """Every existing caller passes none, and must behave exactly as before."""
    assert query_memory._check_optional_deadline(None) is None


def test_the_budget_says_which_unit_it_counts() -> None:
    budget = query_memory._qa_budget()

    assert budget.max_input_tokens == query_memory.QA_DEFAULT_INPUT_BYTES
    assert budget.reserved_output_tokens == query_memory.QA_OUTPUT_RESERVE_BYTES
    assert budget.safety_margin_tokens == query_memory.QA_SAFETY_MARGIN_BYTES


def test_the_context_says_how_much_the_rendering_shed() -> None:
    assert query_memory.GroundedContext.__dataclass_fields__["shed_for_budget"].default == 0


def test_the_first_turn_of_an_entry_is_pruned_like_every_other() -> None:
    content = HEADING + LONG_TURN

    assert evidence_pruning.prunes(content, 0, len(content))


def test_a_piece_with_no_turn_at_all_is_still_delivered_whole() -> None:
    content = b"# A page\n\n" + b"prose without any speaker marker. " * 40

    assert not evidence_pruning.prunes(content, 0, len(content))


def test_a_long_first_sentence_still_leaves_room_for_one_the_question_chose() -> None:
    """The first sentence carries the speaker marker and used to spend the whole budget."""
    first_bytes = len(b"**user:** ") + evidence_pruning.KEEP_BYTES + 50
    spans = [(0, first_bytes), (first_bytes + 1, first_bytes + 30), (first_bytes + 31, first_bytes + 60)]

    chosen = evidence_pruning._within_budget(spans, [0, 2, 1])

    assert chosen == {0, 2}
