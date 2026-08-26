"""An abandoned optional stage must not shut a different kind of stage out.

Measured on the live vault on 2026-08-26, six recall calls in one process at
the 10 s MCP budget: the dense leg reached the answer in one call out of six,
and the refusals were `optional stage capacity exhausted` at 0.00 s -- before
the stage waited for anything. The cause was capacity, not the deadline. One
shared pool of `MAX_OPTIONAL_STRAGGLERS` permits let the abandoned
cross-encoder load of an earlier call, which runs for about 20 s, hold the
permits the dense leg of every later call needed.

These tests drive the two product call sites, `_call_dense` and `_run_reranker`,
rather than the boundary underneath them, so they fail on the behaviour the
vault measured rather than on a keyword argument.
"""
from __future__ import annotations

import contextlib
import threading
import time

import pytest

# A stage abandoned this fast leaves a straggler holding its permit; the
# boundary grants a stage half of what is left, so the wait is half of this.
_ABANDON_SECONDS = 0.1

# What a stage that is meant to be admitted may spend. Generous on purpose: a
# refusal inside this is a refusal of admission, never a deadline.
_ADMITTED_SECONDS = 30.0

# How long a straggler may take to notice the release and free its permit. A
# bound, not a measurement: the work it is released into is a bare return.
_DRAIN_SECONDS = 10.0

_EXHAUSTED = "optional stage capacity exhausted"


def _stall(release: threading.Event):
    def blocked(*_args, **_kwargs):
        release.wait(_DRAIN_SECONDS)
        return []

    return blocked


def _run_rerank(retrieval, seconds: float):
    return retrieval._run_reranker(
        [],
        query="q",
        pool_limit=1,
        deadline_monotonic=time.monotonic() + seconds,
        cancelled=None,
    )


def _run_dense(retrieval, backend, seconds: float):
    return retrieval._call_dense(
        backend, {}, deadline_monotonic=time.monotonic() + seconds, cancelled=None
    )


def _abandon(start) -> None:
    """Start one stage and walk away from it, as a real caller does."""
    with contextlib.suppress(TimeoutError):
        start()


def _refusal_of(start) -> str:
    """Why a stage was refused -- proof the permits really are held.

    Saturation has to be asserted, not assumed: a machine slow enough to eat
    `_ABANDON_SECONDS` before the boundary would leave no straggler at all, and
    the test would then pass for the wrong reason.
    """
    with pytest.raises(TimeoutError) as caught:
        start()
    return str(caught.value)


def _owned_permits(retrieval) -> list:
    """Every permit the boundary can hand out, on this test's own semaphores."""
    kinds = getattr(retrieval, "_OPTIONAL_STAGE_KIND_SLOTS", {})
    shared = [retrieval._OPTIONAL_STAGE_SLOTS] * retrieval.MAX_OPTIONAL_STRAGGLERS
    return shared + list(kinds.values())


def _drained(retrieval) -> bool:
    permits = _owned_permits(retrieval)
    taken = [pool for pool in permits if pool.acquire(timeout=_DRAIN_SECONDS)]
    for pool in taken:
        pool.release()
    return len(taken) == len(permits)


@pytest.fixture
def boundary(monkeypatch):
    """A boundary with permits of this test's own.

    The semaphores are process-wide and other suites leave real stragglers on
    them, so each test gets fresh ones -- the same isolation the existing
    optional-boundary tests use -- and hands its own stragglers back at the end.
    """
    import retrieval

    kinds = getattr(retrieval, "OPTIONAL_STAGE_KINDS", ("dense", "rerank"))
    monkeypatch.setattr(
        retrieval,
        "_OPTIONAL_STAGE_SLOTS",
        threading.BoundedSemaphore(retrieval.MAX_OPTIONAL_STRAGGLERS),
    )
    monkeypatch.setattr(
        retrieval,
        "_OPTIONAL_STAGE_KIND_SLOTS",
        {kind: threading.BoundedSemaphore(1) for kind in kinds},
        raising=False,
    )
    release = threading.Event()
    try:
        yield retrieval, release
    finally:
        release.set()
        assert _drained(retrieval), "this test left an optional straggler running"


def test_a_stalled_reranker_does_not_refuse_the_dense_leg(boundary, monkeypatch):
    """The measured defect: call 6 lost its dense leg to call 1's rerank load."""
    retrieval, release = boundary
    monkeypatch.setattr("reranker.rerank", _stall(release))

    for _ in range(retrieval.MAX_OPTIONAL_STRAGGLERS):
        _abandon(lambda: _run_rerank(retrieval, _ABANDON_SECONDS))
    assert _refusal_of(lambda: _run_rerank(retrieval, _ABANDON_SECONDS)) == _EXHAUSTED

    rows = _run_dense(retrieval, lambda **_kw: [{"id": "dense row"}], _ADMITTED_SECONDS)

    assert rows == [{"id": "dense row"}]


def test_a_stalled_dense_leg_does_not_refuse_the_reranker(boundary, monkeypatch):
    """The same guarantee in the other direction; a bulkhead has two sides."""
    retrieval, release = boundary
    monkeypatch.setattr("reranker.rerank", lambda *_a, **_kw: [{"id": "reranked"}])
    stalled = _stall(release)

    for _ in range(retrieval.MAX_OPTIONAL_STRAGGLERS):
        _abandon(lambda: _run_dense(retrieval, stalled, _ABANDON_SECONDS))
    empty = _stall(release)
    assert _refusal_of(lambda: _run_dense(retrieval, empty, _ABANDON_SECONDS)) == _EXHAUSTED

    rows = _run_rerank(retrieval, _ADMITTED_SECONDS)

    assert rows == [{"id": "reranked"}]


def test_one_kind_still_admits_only_one_straggler(boundary, monkeypatch):
    """The bound stayed: no unbounded thread growth, and no doubled model load."""
    retrieval, release = boundary
    started = []
    lock = threading.Lock()

    def counted(*_args, **_kwargs):
        with lock:
            started.append(1)
        release.wait(_DRAIN_SECONDS)
        return []

    monkeypatch.setattr("reranker.rerank", counted)
    for _ in range(retrieval.MAX_OPTIONAL_STRAGGLERS + 2):
        _abandon(lambda: _run_rerank(retrieval, _ABANDON_SECONDS))

    assert len(started) == 1


def test_the_kinds_are_fixed_so_no_call_can_widen_the_thread_bound() -> None:
    """Permits are declared, not minted: an unknown kind falls back to the pool."""
    import retrieval

    slots = retrieval._optional_stage_slots
    assert set(retrieval._OPTIONAL_STAGE_KIND_SLOTS) == set(retrieval.OPTIONAL_STAGE_KINDS)
    assert slots("dense") is not slots("rerank")
    assert slots("invented") is retrieval._OPTIONAL_STAGE_SLOTS
    assert slots(None) is retrieval._OPTIONAL_STAGE_SLOTS
