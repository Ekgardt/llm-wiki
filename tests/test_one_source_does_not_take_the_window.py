"""One source does not take the window while another covers a part of the question.

The 500-question run of 2026-09-18 gave 6.05 of its 12 candidate slots to 1.95
sessions on the multi-session questions it lost, while 1.3 sessions holding part
of the answer got no slot at all. The window now holds one chunk from every
source that covers a distinct part of the question before the lane score fills
it. These tests pin the product path and the properties the per-source quota of
2026-09-19 had and this rule keeps. Research:
`docs/research/2026-09-22-the-window-covers-the-question-first.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval  # noqa: E402

DAILY = "knowledge/daily/2026-09-18.md"


def _chunk(heading: str, start: int, path: str = DAILY) -> retrieval.RetrievalCandidate:
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


def _ids(candidates) -> list[str]:
    return [item.candidate_id for item in candidates]


def _meta(pool: list[tuple[retrieval.RetrievalCandidate, str]]) -> dict[str, dict[str, str]]:
    return {chunk.candidate_id: {"content": text} for chunk, text in pool}


def _ordered(pool, query: str, limit: int | None) -> tuple[retrieval.RetrievalCandidate, ...]:
    return retrieval._cover_then_fill([chunk for chunk, _text in pool], _meta(pool), query, limit)


def _row(heading: str, start: int, score: float, text: str) -> dict[str, object]:
    return {
        "candidate_id": f"{DAILY}:{heading}:{start}",
        "parent_id": DAILY,
        "relative_path": DAILY,
        "heading_path": (heading,),
        "source_sha256": "a" * 64,
        "byte_start": start,
        "byte_end": start + 10,
        "score": score,
        "content": text,
    }


def test_a_source_covering_a_new_part_takes_the_place_of_a_repeat() -> None:
    """The product path: two turns of one session about the needle, one of another about the thread."""
    hits = [
        _row("Session one", 0, 9.0, "the needle"),
        _row("Session one", 100, 8.0, "the needle again"),
        _row("Session two", 200, 6.0, "the thread"),
    ]

    result = retrieval.retrieve(
        "needle thread",
        requested_profile="BASE",
        limit=2,
        lexical_backend=lambda **_kwargs: hits,
        corpus_generation="gen-cover",
    )

    assert [item.heading_path[0] for item in result.candidates] == ["Session one", "Session two"]


def test_one_session_alone_still_fills_the_window_end_to_end() -> None:
    """The direction a source rule breaks first: the whole answer inside one session."""
    hits = [_row("Session one", start, 9.0 - start / 100, "needle") for start in (0, 100, 200, 300)]

    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        limit=3,
        lexical_backend=lambda **_kwargs: hits,
        corpus_generation="gen-one-source",
    )

    assert [item.byte_start for item in result.candidates] == [0, 100, 200]


def test_a_pool_holding_one_source_keeps_the_order_it_had() -> None:
    """Nothing to make room for, so the rule must not move anything."""
    pool = [(_chunk("Session one", start), "needle thread") for start in (0, 100, 200, 300, 400)]

    assert _ids(_ordered(pool, "needle thread", 2)) == _ids(chunk for chunk, _ in pool)


def test_a_pool_that_fits_the_window_is_untouched() -> None:
    """Every source already holds a place, with a window or without one."""
    pool = [
        (_chunk("Session one", 0), "needle"),
        (_chunk("Session one", 100), "needle"),
        (_chunk("Session two", 200), "thread"),
    ]
    expected = _ids(chunk for chunk, _ in pool)

    assert (_ids(_ordered(pool, "needle thread", 3)), _ids(_ordered(pool, "needle thread", None))) == (
        expected,
        expected,
    )


def test_a_source_with_one_chunk_in_the_window_keeps_its_place() -> None:
    """Only a chunk of a source that keeps another chunk inside is ever displaced."""
    alone = (_chunk("Session two", 200), "nothing here")
    starved = [(_chunk("Session one", 0), "needle"), alone, (_chunk("Session three", 300), "thread")]
    roomy = [(_chunk("Session one", 0), "needle"), (_chunk("Session one", 100), "needle"), alone,
             (_chunk("Session three", 300), "thread")]

    kept = _ordered(starved, "needle thread", 2)
    made_room = _ordered(roomy, "needle thread", 3)

    assert _ids(kept) == _ids(chunk for chunk, _ in starved)
    assert [item.heading_path[0] for item in made_room[:3]] == ["Session one", "Session two", "Session three"]


def test_the_chunks_of_one_source_keep_their_order_except_the_one_admitted() -> None:
    """A later chunk passes its own earlier ones only when it covers what they do not."""
    pool = [
        (_chunk("Session one", 0), "needle"),
        (_chunk("Session two", 50), "other"),
        (_chunk("Session one", 100), "needle"),
        (_chunk("Session one", 200), "needle and thread"),
    ]

    ordered = _ordered(pool, "needle thread", 3)
    ones = [item.byte_start for item in ordered if item.heading_path == ("Session one",)]

    assert [item.byte_start for item in ordered[:3]] == [0, 50, 200]
    assert ones == [0, 200, 100]


def test_with_equal_coverage_the_lane_best_chunk_is_the_one_admitted() -> None:
    """Ties go to the lane order, so a source never overtakes itself for nothing."""
    pool = [
        (_chunk("Session two", 0), "x"),
        (_chunk("Session two", 50), "y"),
        (_chunk("Session one", 100), "needle"),
        (_chunk("Session one", 200), "needle"),
    ]

    ordered = _ordered(pool, "needle", 2)

    assert [item.byte_start for item in ordered] == [0, 100, 50, 200]


def test_the_rule_defers_and_never_drops() -> None:
    """A rearrangement, not a filter: every chunk is still in the list, once."""
    pool = [(_chunk("Session one", start), "needle") for start in (0, 100, 200)]
    pool.append((_chunk("Session two", 300), "thread"))

    ordered = _ordered(pool, "needle thread", 2)

    assert sorted(_ids(ordered)) == sorted(_ids(chunk for chunk, _ in pool))
    assert [item.byte_start for item in ordered] == [0, 300, 100, 200]
