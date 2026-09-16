"""One score over what each lane said about a candidate, and what it did not.

Rank fusion is a vote: a candidate one lane never returned is treated as if that lane had
ranked it last, so a turn the dense lane ranks first loses to a turn both lanes rank low.
Measured on this vault's stand (189 LongMemEval questions, 2026-09-16): the union of the
lanes holds every evidence turn for 188 of them, today's fused order delivers 0.698 of them
inside the reader's twelve, and this score delivers 0.794 — 0.815 once the cross-encoder has
spoken — losing four questions and winning twenty-two. A lane's silence was measured
slightly *positive* for the lexical lane, which a vote cannot express.

The weights are a logistic fit over labelled questions, fitted offline and shipped as
constants; nothing is trained at query time. Research:
`docs/research/2026-09-16-one-score-over-the-lanes.md`.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# The fit of 2026-09-16 (189 questions, five-fold cross-validated). The intercept is left
# out: a constant cannot change an order.
LEXICAL_RANK = -0.5071
DENSE_RANK = -0.7639
LEXICAL_SILENT = 0.3052
DENSE_SILENT = -1.1359
USER_TURN = 2.5144
RERANK_SCORE = 0.2789
RERANK_APPLIED = 3.0138
# A rank past this is as far away as the fit ever saw; the log keeps one lane's depth from
# drowning the other signals.
FURTHEST_RANK = 1000

USER_MARKER = "**user:**"
ASSISTANT_MARKER = "**assistant:**"


def _log_rank(rank: int | None) -> float:
    if not rank or rank <= 0:
        return math.log(FURTHEST_RANK)
    return math.log(min(int(rank), FURTHEST_RANK))


def _silent(rank: int | None) -> float:
    if not rank or rank <= 0:
        return 1.0
    return 0.0


def is_user_turn(text: str) -> bool:
    """Whether this chunk begins with the user's own words.

    Evidence lives in what the person said: 91 % of the evidence turns of the stand are
    user turns, against 40 % of the candidates.
    """
    user_at = text.find(USER_MARKER)
    if user_at < 0:
        return False
    assistant_at = text.find(ASSISTANT_MARKER)
    return assistant_at < 0 or user_at < assistant_at


def _rerank_terms(rerank_score: float | None) -> float:
    if rerank_score is None:
        return 0.0
    return RERANK_SCORE * float(rerank_score) + RERANK_APPLIED


def score(
    *,
    lexical_rank: int | None,
    dense_rank: int | None,
    rerank_score: float | None,
    text: str,
) -> float:
    """How much the lanes, their silences and the turn's own voice argue for this chunk."""
    return (
        LEXICAL_RANK * _log_rank(lexical_rank)
        + DENSE_RANK * _log_rank(dense_rank)
        + LEXICAL_SILENT * _silent(lexical_rank)
        + DENSE_SILENT * _silent(dense_rank)
        + USER_TURN * float(is_user_turn(text))
        + _rerank_terms(rerank_score)
    )


def candidate_text(candidate: Any, display_meta: Mapping[str, Mapping[str, Any]]) -> str:
    info = display_meta.get(candidate.candidate_id) or {}
    return str(info.get("content") or info.get("summary") or "")


def candidate_score(candidate: Any, display_meta: Mapping[str, Mapping[str, Any]]) -> float:
    return score(
        lexical_rank=candidate.bm25_rank,
        dense_rank=candidate.vector_rank,
        rerank_score=candidate.rerank_score,
        text=candidate_text(candidate, display_meta),
    )
