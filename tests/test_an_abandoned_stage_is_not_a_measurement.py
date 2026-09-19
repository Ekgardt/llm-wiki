"""A stage that gave up on its own deadline does not teach the cost model what it cost.

Third audit, 2026-09-17 (retrieval L2). A rerank cut by its deadline returns the fused order
normally, so the worker used to time it and record that as the cost of a rerank — a number
bounded by the budget the stage was given, never by the work. Admission then always said
"fits", and a warm reranker needing more than its share was waited for and abandoned on every
call. See `docs/research/2026-09-17-an-abandoned-stage-is-not-a-measurement.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval  # noqa: E402


@pytest.fixture(autouse=True)
def forget_observations():
    with retrieval._OPTIONAL_STAGE_OBSERVED_LOCK:
        retrieval._OPTIONAL_STAGE_OBSERVED.clear()
    yield
    with retrieval._OPTIONAL_STAGE_OBSERVED_LOCK:
        retrieval._OPTIONAL_STAGE_OBSERVED.clear()


def _abandoned() -> list[dict]:
    return [{"reranker_applied": False, "reranker_fallback_reason": "reranker_deadline"}]


def _scored() -> list[dict]:
    return [{"reranker_applied": True, "reranker_fallback_reason": None}]


def _ran(value, observes) -> object:
    """One bounded optional run of kind "rerank", waited for to the end."""
    return retrieval._run_optional_bounded(
        lambda: value,
        deadline=time.monotonic() + retrieval.OPTIONAL_STAGE_MAX_SECONDS,
        cancelled=None,
        kind="rerank",
        observes=observes,
    )


def _generous_deadline() -> float:
    """Far enough out that the stage's share of it covers the whole ceiling.

    The share, not the caller's budget, is what an unmeasured kind is admitted
    against, so a caller offering exactly the ceiling would be refused the wait.
    """
    ceiling = retrieval.OPTIONAL_STAGE_MAX_SECONDS + retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS
    return time.monotonic() + ceiling / retrieval.OPTIONAL_STAGE_BUDGET_SHARE


def _through_the_product(monkeypatch, documents: list[dict]) -> object:
    """The product's own deadline-bounded rerank path, with the model replaced."""
    import reranker

    monkeypatch.setattr(reranker, "rerank", lambda *args, **kwargs: documents)
    return retrieval._run_reranker(
        [{"content": "a"}],
        query="q",
        pool_limit=10,
        deadline_monotonic=_generous_deadline(),
        cancelled=None,
    )


def test_a_rerank_that_gave_up_on_its_deadline_records_no_cost(monkeypatch) -> None:
    returned = _through_the_product(monkeypatch, _abandoned())

    assert returned == _abandoned()
    assert retrieval._observed_optional_stage_cost("rerank") is None


def test_a_rerank_that_scored_records_its_cost(monkeypatch) -> None:
    _through_the_product(monkeypatch, _scored())

    assert retrieval._observed_optional_stage_cost("rerank") is not None


def test_an_unmeasured_kind_is_waited_for_only_when_the_whole_ceiling_is_on_offer() -> None:
    """With no usable observation the existing conservative rule decides the wait."""
    ceiling = retrieval.OPTIONAL_STAGE_MAX_SECONDS
    _ran(_abandoned(), retrieval._rerank_scored)
    now = time.monotonic()

    fits = (
        retrieval._optional_stage_fits("rerank", now + ceiling),
        retrieval._optional_stage_fits("rerank", now + ceiling / 3),
    )

    assert fits == (True, False)


def test_a_stage_with_no_verdict_of_its_own_is_measured_by_every_finished_run() -> None:
    """The dense leg returns rows, not a verdict, and keeps the old behaviour."""
    assert retrieval._every_value_measures(object()) is True
    _ran([{"id": "a"}], retrieval._every_value_measures)
    assert retrieval._observed_optional_stage_cost("rerank") is not None
