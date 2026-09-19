"""A turn of a conversation keeps its rank; only a compiled page's later chunks wait.

One slot per session pushed every later turn of a session behind the first chunk of
every other session, and on LongMemEval the turn holding the answer went from score
rank 2-11 to rank 56-80. Research:
`docs/research/2026-09-15-what-a-slot-should-reward.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval  # noqa: E402


def _chunk(path: str, heading: tuple[str, ...], start: int) -> retrieval.RetrievalCandidate:
    return retrieval.RetrievalCandidate(
        candidate_id=f"{path}:{heading}:{start}",
        parent_id=path,
        relative_path=path,
        heading_path=heading,
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


@pytest.mark.parametrize("episode", ["knowledge/daily/2023-05-26.md", "knowledge/raw/sessions/2023-05-26/s1.md"])
def test_the_second_turn_of_a_session_is_not_sent_behind_other_sessions(episode: str) -> None:
    ranked = [
        _chunk(episode, ("session one",), 0),
        _chunk(episode, ("session two",), 100),
        _chunk(episode, ("session one",), 50),
        _chunk("knowledge/notes/page.md", ("First",), 0),
        _chunk("knowledge/notes/page.md", ("Second",), 50),
    ]

    assert _ids(retrieval._page_diverse(ranked)) == _ids(ranked)


def test_a_compiled_page_still_waits_behind_the_turns_that_outranked_it() -> None:
    first, second, turn = (
        _chunk("knowledge/notes/page.md", ("First",), 0),
        _chunk("knowledge/notes/page.md", ("Second",), 50),
        _chunk("knowledge/daily/2023-05-26.md", ("session one",), 0),
    )

    assert _ids(retrieval._page_diverse([first, second, turn])) == _ids([first, turn, second])
