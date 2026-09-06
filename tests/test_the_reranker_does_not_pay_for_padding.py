"""A short passage must not be padded up to the longest one in the batch.

Measured on the live vault on 2026-09-06: the twenty candidates of a real
query tokenize to between 61 and 512 tokens, and scoring them as one batch
padded all twenty to 512 — half the arithmetic was padding. One query took
17.0 seconds that way and 7.15 seconds in length-sorted batches of two, with
the scores agreeing to 1e-5.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reranker  # noqa: E402


def _pairs(lengths):
    return [("query", "x" * length) for length in lengths]


def test_batches_hold_passages_of_similar_length():
    batches = reranker._batches_by_length(_pairs([500, 60, 400, 70]))

    lengths = [[len(_pairs([500, 60, 400, 70])[i][1]) for i in b] for b in batches]
    assert lengths == [[60, 70], [400, 500]]


def test_every_pair_is_scored_exactly_once():
    pairs = _pairs([500, 60, 400, 70, 90])

    batches = reranker._batches_by_length(pairs)

    assert sorted(index for batch in batches for index in batch) == list(range(5))


def test_a_score_comes_back_to_the_position_its_pair_came_from(monkeypatch):
    pairs = _pairs([500, 60, 400])
    monkeypatch.setattr(
        reranker,
        "_batch_logits",
        lambda bundle, chosen: [float(len(pair[1])) for pair in chosen],
    )

    scores = reranker._cross_encoder_scores({}, pairs)

    assert scores == [500.0, 60.0, 400.0]


def test_an_empty_candidate_list_scores_to_nothing(monkeypatch):
    monkeypatch.setattr(
        reranker, "_batch_logits", lambda bundle, chosen: [0.0] * len(chosen)
    )

    assert reranker._cross_encoder_scores({}, []) == []
