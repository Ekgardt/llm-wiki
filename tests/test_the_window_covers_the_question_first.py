"""The reader's window covers the parts of the question before the lane score fills it.

The parts are read from the question and the store only: its terms, the stretches
of days written into it, each turn's fact keys, and one part per source that
mentions a counted kind. A compiled page the coverage admits pulls in the source
blocks and pages it names. Research:
`docs/research/2026-09-22-the-window-covers-the-question-first.md`.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fact_keys  # noqa: E402
import memory_state  # noqa: E402
import retrieval  # noqa: E402

DAY_ONE = "knowledge/daily/2023-05-20.md"
DAY_TWO = "knowledge/daily/2023-05-22.md"
DAY_FAR = "knowledge/daily/2023-05-25.md"
PAGE = "knowledge/notes/needle-page.md"
OTHER = "knowledge/notes/Other Page.md"


def _chunk(path: str, heading: str, start: int) -> retrieval.RetrievalCandidate:
    return retrieval.RetrievalCandidate(
        candidate_id=f"{path}:{heading}:{start}",
        parent_id=path,
        relative_path=path,
        heading_path=(heading,),
        source_sha256="a" * 64,
        byte_start=start,
        byte_end=start + 10,
        bm25_rank=1,
        bm25_score=1.0,
        vector_rank=None,
        vector_score=None,
        graph_rank=None,
        graph_score=None,
        rrf_score=1.0,
        rerank_score=None,
        final_score=1.0,
        evidence_ids=(),
    )


def _meta(pool: list[tuple[retrieval.RetrievalCandidate, dict]]) -> dict[str, dict]:
    return {chunk.candidate_id: info for chunk, info in pool}


def _window(pool, query: str, limit: int) -> list[str]:
    ordered = retrieval._cover_then_fill([chunk for chunk, _ in pool], _meta(pool), query, limit)
    return [item.candidate_id for item in ordered[:limit]]


def _turn(path: str, start: int, text: str, span: str = "") -> tuple[retrieval.RetrievalCandidate, dict]:
    return _chunk(path, "session", start), {"content": text, "span_sha256": span}


def test_a_stretch_of_days_is_one_part_and_separate_days_are_two() -> None:
    """Three consecutive days written by the anchor are one range; a day apart is another."""
    inside = [_turn(DAY_ONE, 0, "the needle"), _turn(DAY_ONE, 100, "the needle again")]
    stretch = [*inside, _turn(DAY_TWO, 0, "something else")]
    apart = [*inside, _turn(DAY_FAR, 0, "something else")]

    assert _window(stretch, "needle 2023-05-20 2023-05-21 2023-05-22", 2) == [
        inside[0][0].candidate_id, inside[1][0].candidate_id]
    assert _window(apart, "needle 2023-05-20 2023-05-25", 2) == [
        inside[0][0].candidate_id, apart[2][0].candidate_id]


def test_a_count_admits_every_source_that_mentions_the_kind() -> None:
    """Each source mentioning the counted kind is one instance, so it is its own part."""
    pool = [
        (_chunk(DAY_ONE, "one", 0), {"content": "bikes serviced today"}),
        (_chunk(DAY_ONE, "one", 100), {"content": "bikes again"}),
        (_chunk(DAY_ONE, "two", 200), {"content": "my bike squeaks"}),
        (_chunk(DAY_ONE, "three", 300), {"content": "my car is fine"}),
    ]

    window = _window(pool, "How many bikes did I service", 2)

    assert window == [pool[0][0].candidate_id, pool[2][0].candidate_id]


def test_a_fact_key_lets_a_turn_cover_what_its_text_does_not() -> None:
    """The store says the turn states a needle; its own words do not."""
    span = "cover-" + uuid.uuid4().hex
    store = fact_keys.KeyStore(fact_keys.store_path(memory_state.STATE_ROOT))
    try:
        store.add(fact_keys.Turn(DAY_TWO, 0, 10, span, "sewing kit"), ["the person bought a needle"])
    finally:
        store.close()
    inside = [_turn(DAY_ONE, 0, "thread"), _turn(DAY_ONE, 100, "thread again")]
    keyed = [*inside, _turn(DAY_TWO, 0, "sewing kit", span)]
    unkeyed = [*inside, _turn(DAY_TWO, 0, "sewing kit", "no-such-span")]

    assert _window(keyed, "needle thread", 2) == [inside[0][0].candidate_id, keyed[2][0].candidate_id]
    assert _window(unkeyed, "needle thread", 2) == [inside[0][0].candidate_id, inside[1][0].candidate_id]


def _page_text() -> str:
    reference = f"daily:2023-05-20 sha256:{'a' * 64} block:10:00:00 bytes:0-5"
    return f"the needle\n\n## Evidence\n- `{reference}` — said so\n\nSee [[Other Page]].\n"


def test_an_admitted_page_pulls_in_the_block_and_the_page_it_names() -> None:
    """Index-driven completion: the page is the cue, its references retrieve the episode."""
    pool = [
        (_chunk(PAGE, "Lesson", 0), {"content": _page_text()}),
        (_chunk(PAGE, "Lesson", 100), {"content": "more of the page"}),
        (_chunk(PAGE, "Lesson", 200), {"content": "and more"}),
        (_chunk(DAY_ONE, "[10:00:00] session_end | s1", 0), {"content": "zzz"}),
        (_chunk(OTHER, "Top", 0), {"content": "zzz"}),
    ]

    window = _window(pool, "needle", 3)

    assert window == [pool[0][0].candidate_id, pool[3][0].candidate_id, pool[4][0].candidate_id]


def test_an_episode_points_at_nothing() -> None:
    """Only a compiled page carries an index; a turn quoting a reference pulls nothing."""
    pool = [
        (_chunk(DAY_TWO, "[09:00:00] session_end | s0", 0), {"content": _page_text()}),
        (_chunk(DAY_TWO, "[09:00:00] session_end | s0", 100), {"content": "needle"}),
        (_chunk(DAY_ONE, "[10:00:00] session_end | s1", 0), {"content": "zzz"}),
    ]

    assert _window(pool, "needle", 2) == [pool[0][0].candidate_id, pool[1][0].candidate_id]


def test_the_selection_stops_at_zero_gain() -> None:
    """A source that covers nothing new is not admitted, however many chunks the first has."""
    pool = [_turn(DAY_ONE, 0, "needle"), _turn(DAY_ONE, 100, "needle"), _turn(DAY_TWO, 0, "needle too")]

    assert _window(pool, "needle", 2) == [pool[0][0].candidate_id, pool[1][0].candidate_id]


def test_more_admitted_than_can_be_displaced_leaves_the_lane_last_outside() -> None:
    """One displaceable repeat inside, two covering sources outside: the lane-first comes in."""
    pool = [
        _turn(DAY_ONE, 0, "alpha"), _turn(DAY_ONE, 100, "alpha"),
        _turn(DAY_TWO, 0, "beta"), _turn(DAY_FAR, 0, "gamma"),
    ]

    ordered = retrieval._cover_then_fill([c for c, _ in pool], _meta(pool), "alpha beta gamma", 2)

    assert [item.relative_path for item in ordered] == [DAY_ONE, DAY_TWO, DAY_ONE, DAY_FAR]
