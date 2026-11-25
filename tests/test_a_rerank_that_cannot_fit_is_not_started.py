"""A rerank learns its cost once, and one known not to fit is not started.

See docs/research/2026-09-25-a-rerank-that-cannot-fit-is-not-started.md.
"""

from __future__ import annotations

import time

import pytest
import retrieval

from tests.clocks import frozen_clock


@pytest.fixture(autouse=True)
def _no_observed_cost(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_OBSERVED", {})


def test_an_unknown_cost_gets_the_ceiling_to_finish_and_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(retrieval, "time", frozen_clock(now))

    worker = retrieval._rerank_worker_deadline(now + 3.0)

    assert worker == now + retrieval.OPTIONAL_STAGE_MAX_SECONDS


def test_a_cost_known_not_to_fit_is_not_started() -> None:
    retrieval._observe_optional_stage("rerank", 8.0)

    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval._rerank_worker_deadline(time.monotonic() + 3.0)


def test_a_cost_that_fits_keeps_the_callers_window() -> None:
    retrieval._observe_optional_stage("rerank", 0.5)
    window = time.monotonic() + 3.0

    assert retrieval._rerank_worker_deadline(window) == window
