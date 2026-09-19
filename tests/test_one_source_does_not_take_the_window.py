"""One source does not take the window while another source has none.

The 500-question run of 2026-09-18 gave 6.05 of its 12 candidate slots to 1.95
sessions on the multi-session questions it lost, while 1.3 sessions holding part
of the answer got no slot at all. The packer already spends its budget
coverage-first; these tests pin the same rule one step earlier, where the twelve
candidates are chosen, and pin the two directions it must not break.
Research: `docs/research/2026-09-19-one-session-does-not-take-the-window.md`.
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


def _row(heading: str, start: int, score: float) -> dict[str, object]:
    return {
        "candidate_id": f"{DAILY}:{heading}:{start}",
        "parent_id": DAILY,
        "relative_path": DAILY,
        "heading_path": (heading,),
        "source_sha256": "a" * 64,
        "byte_start": start,
        "byte_end": start + 10,
        "score": score,
    }


def test_a_third_turn_of_one_session_waits_for_a_session_with_no_slot() -> None:
    """The product path: three turns of one session, one turn of another, three slots."""
    hits = [
        _row("Session one", 0, 9.0),
        _row("Session one", 100, 8.0),
        _row("Session one", 200, 7.0),
        _row("Session two", 300, 6.0),
    ]

    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        limit=3,
        lexical_backend=lambda **_kwargs: hits,
        corpus_generation="gen-source-quota",
    )

    visible = [item.heading_path[0] for item in result.candidates]
    assert visible == ["Session one", "Session one", "Session two"]


def test_one_session_alone_still_fills_the_window_end_to_end() -> None:
    """The direction a quota breaks first: the whole answer inside one session."""
    hits = [_row("Session one", start, 9.0 - start / 100) for start in (0, 100, 200, 300)]

    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        limit=3,
        lexical_backend=lambda **_kwargs: hits,
        corpus_generation="gen-one-source",
    )

    assert [item.byte_start for item in result.candidates] == [0, 100, 200]


def test_a_pool_holding_one_source_keeps_the_order_it_had() -> None:
    """Nothing to make room for, so the quota must not move anything."""
    ranked = [_chunk("Session one", start) for start in (0, 100, 200, 300, 400)]

    assert _ids(retrieval._source_quota(ranked)) == _ids(ranked)


def test_a_source_with_one_chunk_keeps_its_place() -> None:
    """The quota never defers a source's first chunk, so an only chunk cannot fall."""
    only = _chunk("Session two", 300)
    ranked = [_chunk("Session one", 0), _chunk("Session one", 100), only]

    ordered = retrieval._source_quota(ranked)

    assert _ids(ordered) == _ids(ranked)
    assert ordered[-1].candidate_id == only.candidate_id


def test_the_chunks_of_one_source_keep_their_order() -> None:
    """Within the rule the lane score still decides: a source is never re-sorted."""
    ranked = [
        _chunk("Session one", 0),
        _chunk("Session one", 100),
        _chunk("Session one", 200),
        _chunk("Session two", 300),
        _chunk("Session one", 400),
    ]

    ordered = retrieval._source_quota(ranked)
    starts = [item.byte_start for item in ordered if item.heading_path == ("Session one",)]

    assert starts == [0, 100, 200, 400]
    assert _ids(ordered) == _ids([ranked[0], ranked[1], ranked[3], ranked[2], ranked[4]])


def test_the_quota_defers_and_never_drops() -> None:
    """A demotion, not a filter: every chunk is still in the list, once."""
    ranked = [_chunk("Session one", start) for start in (0, 100, 200)]
    ranked.append(_chunk("Session two", 300))

    ordered = retrieval._source_quota(ranked)

    assert sorted(_ids(ordered)) == sorted(_ids(ranked))
    assert _ids(ordered) == _ids([ranked[0], ranked[1], ranked[3], ranked[2]])
