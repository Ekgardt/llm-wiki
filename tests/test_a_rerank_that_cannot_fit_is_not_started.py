"""A rerank learns its cost once, and one known not to fit is not started.

See docs/research/2026-09-25-a-rerank-that-cannot-fit-is-not-started.md.
"""

from __future__ import annotations

import time

import pytest
import retrieval
from llm_wiki import retrieval_optional_stages


@pytest.fixture(autouse=True)
def _no_observed_cost(monkeypatch) -> None:
    monkeypatch.setattr("llm_wiki.retrieval_optional_stages.OPTIONAL_STAGE_OBSERVED", {})


def test_an_unknown_cost_gets_the_ceiling_to_finish_and_be_measured() -> None:
    window = time.monotonic() + 3.0

    worker = retrieval.rerank_worker_deadline(window)

    assert worker - time.monotonic() > retrieval_optional_stages.OPTIONAL_STAGE_MAX_SECONDS - 1


def test_a_cost_known_not_to_fit_is_not_started() -> None:
    retrieval.observe_optional_stage("rerank", 8.0)

    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval.rerank_worker_deadline(time.monotonic() + 3.0)


def test_a_cost_that_fits_keeps_the_callers_window() -> None:
    retrieval.observe_optional_stage("rerank", 0.5)
    window = time.monotonic() + 3.0

    assert retrieval.rerank_worker_deadline(window) == window
