"""A slow rerank is tried again later, and an absent one says so (audit 2026-09-26 B-16).

docs/research/2026-09-26-a-rerank-is-tried-again.md
"""
from __future__ import annotations

import time

import pytest
import reranker
import retrieval

DOCS = [{"rrf_score": 1.0}, {"rrf_score": 0.5}]


@pytest.fixture(autouse=True)
def _fresh_costs(monkeypatch):
    monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_OBSERVED", {})
    monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_OBSERVED_AT", {})


def test_a_cost_that_does_not_fit_is_tried_again_once_it_is_old(monkeypatch) -> None:
    retrieval._observe_optional_stage("rerank", 9.0)
    window = time.monotonic() + 5

    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval._rerank_worker_deadline(window)
    monkeypatch.setitem(
        retrieval._OPTIONAL_STAGE_OBSERVED_AT,
        "rerank",
        time.monotonic() - retrieval.OPTIONAL_STAGE_REPROBE_SECONDS - 1,
    )

    assert retrieval._rerank_worker_deadline(window) > window


def test_an_absent_reranker_is_named_not_timed_out(monkeypatch) -> None:
    monkeypatch.setattr(reranker, "_libraries_importable", lambda: False)

    assert reranker.should_rerank(profile="HYBRID", candidates=DOCS) == (False, "reranker_unavailable")
