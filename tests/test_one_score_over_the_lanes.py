"""A turn one lane never returned still wins its place, and a page's order is untouched.

Rank fusion treats a silent lane as a vote against, so a user turn the dense lane ranked
first lost to an assistant turn both lanes ranked low. Research:
`docs/research/2026-09-16-one-score-over-the-lanes.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lane_score  # noqa: E402
import retrieval  # noqa: E402

DAILY = "knowledge/daily/2023-05-26.md"


def _candidate(name: str, path: str, lexical: int | None, dense: int | None) -> retrieval.RetrievalCandidate:
    return retrieval.RetrievalCandidate(
        candidate_id=name,
        parent_id=path,
        relative_path=path,
        heading_path=("session one",),
        source_sha256="a" * 64,
        byte_start=0,
        byte_end=10,
        bm25_rank=lexical,
        bm25_score=1.0,
        vector_rank=dense,
        vector_score=1.0,
        graph_rank=None,
        graph_score=None,
        rrf_score=1.0,
        rerank_score=None,
        final_score=1.0,
        evidence_ids=(),
    )


def _meta(**texts: str) -> dict[str, dict[str, str]]:
    return {name: {"content": text} for name, text in texts.items()}


def test_the_user_turn_only_the_dense_lane_found_leads_the_assistant_turn_both_found() -> None:
    assistant = _candidate("assistant", DAILY, 1, 3)
    user = _candidate("user", DAILY, None, 1)
    meta = _meta(assistant="**assistant:** here is a long helpful reply about theatres",
                 user="**user:** the play I attended was The Glass Menagerie")

    ordered = retrieval._evidence_ordered([assistant, user], meta)

    assert [item.candidate_id for item in ordered] == ["user", "assistant"]


def test_a_compiled_page_keeps_the_place_the_trust_table_gave_it() -> None:
    page = _candidate("page", "knowledge/notes/decision.md", 5, None)
    turn = _candidate("turn", DAILY, 2, 2)
    meta = _meta(page="a decision page", turn="**assistant:** a reply")

    ordered = retrieval._evidence_ordered([page, turn], meta)

    assert [item.candidate_id for item in ordered] == ["page", "turn"]


def test_the_score_reads_a_silent_lane_as_slightly_for_the_candidate() -> None:
    last = lane_score.score(lexical_rank=lane_score.FURTHEST_RANK, dense_rank=5, rerank_score=None,
                            text="**assistant:** reply")
    silent = lane_score.score(lexical_rank=None, dense_rank=5, rerank_score=None, text="**assistant:** reply")

    assert silent > last
    assert lane_score.is_user_turn("**user:** mine\n**assistant:** theirs") is True
