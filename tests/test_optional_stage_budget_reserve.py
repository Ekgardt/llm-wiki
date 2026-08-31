"""An optional stage may not spend the budget the mandatory tail needs.

Measured on the live vault before this was true: the dense and rerank stages
together spent 4.4-5.5 s of a 10 s operation and returned nothing, the deadline
then fell during the mandatory tail, and the lexical answer that had already
been computed was discarded. 18 of 36 calls across three runs under load raised
instead of answering; not one returned a degraded answer.

See `docs/research/2026-08-29-what-an-optional-stage-may-spend.md`.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import retrieval  # noqa: E402


@pytest.fixture(autouse=True)
def _forget_observed_costs():
    """Each test starts with nothing observed; the table is process-global."""
    with retrieval._OPTIONAL_STAGE_OBSERVED_LOCK:
        saved = dict(retrieval._OPTIONAL_STAGE_OBSERVED)
        retrieval._OPTIONAL_STAGE_OBSERVED.clear()
    yield
    with retrieval._OPTIONAL_STAGE_OBSERVED_LOCK:
        retrieval._OPTIONAL_STAGE_OBSERVED.clear()
        retrieval._OPTIONAL_STAGE_OBSERVED.update(saved)


def test_an_optional_stage_never_runs_into_the_mandatory_tail():
    """The granted window always ends a full reserve before the operation does."""
    deadline = time.monotonic() + 10.0
    granted = retrieval._optional_stage_deadline(deadline)
    assert granted <= deadline - retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS


def test_the_reserve_binds_when_the_share_alone_would_not():
    """With little left, the share would still hand over most of it. The reserve does not.

    This is the case that lost the answer: two stages in sequence take half,
    then half of the rest, and neither owes the tail anything.
    """
    deadline = time.monotonic() + 3.0
    share_only = time.monotonic() + 3.0 * retrieval.OPTIONAL_STAGE_BUDGET_SHARE
    granted = retrieval._optional_stage_deadline(deadline)
    assert granted < share_only
    assert granted <= deadline - retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS


def test_a_stage_with_no_time_left_is_still_started_but_not_waited_for():
    """Warming is never refused; only spending a budget that cannot buy a result.

    A caller too late to use the answer still leaves the model resident and the
    cost recorded, which is what makes the next caller cheap.
    """
    deadline = time.monotonic() + 0.5
    granted = retrieval._optional_stage_deadline(deadline)
    started = threading.Event()
    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval._run_optional_bounded(
            lambda: (started.set(), "value")[1],
            deadline=granted,
            cancelled=None,
            kind="dense",
        )
    assert started.wait(5.0)


def test_a_generous_caller_keeps_a_window_far_wider_than_the_reserve():
    """A CLI granting minutes is unaffected by the reserve.

    The ceiling itself is applied later, inside the wait, so what the reserve
    must not do here is bite: half of ten minutes is still minutes.
    """
    deadline = time.monotonic() + 600.0
    granted = retrieval._optional_stage_deadline(deadline)
    window = granted - time.monotonic()
    assert window > retrieval.OPTIONAL_STAGE_MAX_SECONDS
    assert window == pytest.approx(300.0, abs=1.0)


def test_a_finished_run_records_what_it_cost():
    retrieval._run_optional_bounded(
        lambda: "value",
        deadline=time.monotonic() + retrieval.OPTIONAL_STAGE_MAX_SECONDS,
        cancelled=None,
        kind="dense",
    )
    assert retrieval._observed_optional_stage_cost("dense") is not None


def test_a_failed_run_is_not_recorded_as_a_cheap_one():
    """A fast failure is not evidence that the work is cheap."""

    def explode():
        raise RuntimeError("no")

    retrieval._observe_optional_stage("dense", 0.01)
    with pytest.raises(RuntimeError):
        retrieval._run_optional_bounded(
            explode, deadline=time.monotonic() + 5.0, cancelled=None, kind="dense"
        )
    # Still the figure from the run that produced something, not the failure's.
    assert retrieval._observed_optional_stage_cost("dense") == 0.01


def test_a_kind_observed_to_be_slower_than_the_window_is_not_waited_for():
    retrieval._observe_optional_stage("dense", 9.0)
    started = threading.Event()
    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval._run_optional_bounded(
            lambda: (started.set(), "value")[1],
            deadline=time.monotonic() + 2.0,
            cancelled=None,
            kind="dense",
        )
    # The worker still ran: skipping the wait must not skip warming the cache,
    # which is what makes the next call cheap.
    assert started.wait(5.0)


def test_a_kind_observed_to_fit_is_waited_for():
    retrieval._observe_optional_stage("dense", 0.01)
    value = retrieval._run_optional_bounded(
        lambda: "value", deadline=time.monotonic() + 5.0, cancelled=None, kind="dense"
    )
    assert value == "value"


def test_an_unknown_kind_is_not_waited_for_on_an_operation_sized_budget():
    """The MCP case: ~3.5 s on offer against a measured 10.13 s cold model load.

    Waiting cannot succeed, and it does not have to -- the worker is started
    anyway and the straggler that finishes it records the cost for the next
    call.
    """
    started = threading.Event()
    with pytest.raises(retrieval.OptionalStageTimeout):
        retrieval._run_optional_bounded(
            lambda: (started.set(), "value")[1],
            deadline=time.monotonic() + 3.5,
            cancelled=None,
            kind="dense",
        )
    assert started.wait(5.0)


def test_an_unknown_kind_is_waited_for_when_the_caller_granted_the_ceiling():
    """The one-shot CLI case the ceiling already exists for: no second call to be warm for."""
    value = retrieval._run_optional_bounded(
        lambda: "value",
        deadline=time.monotonic() + retrieval.OPTIONAL_STAGE_MAX_SECONDS,
        cancelled=None,
        kind="dense",
    )
    assert value == "value"


def test_a_pathological_run_is_recorded_as_does_not_fit_not_as_its_length():
    """Found by a real cross-suite failure, not by reading the code.

    `test_mcp_server.py` stalls a backend for about 30 s on purpose. Recorded
    literally, that figure then governed every later caller, including ones
    whose window was 15 s -- wide enough for any real stage. The comparison can
    never spend more than the ceiling, so anything above it means one thing.
    """
    retrieval._observe_optional_stage("dense", 30.0)
    assert retrieval._observed_optional_stage_cost("dense") == (
        retrieval.OPTIONAL_STAGE_MAX_SECONDS
    )
    generous = time.monotonic() + retrieval.OPTIONAL_STAGE_MAX_SECONDS + 3.0
    assert retrieval._optional_stage_fits("dense", generous)


def test_unlabelled_optional_work_is_still_admitted():
    """The cost model is per kind; work with no kind has nothing to be modelled against."""
    value = retrieval._run_optional_bounded(
        lambda: "value", deadline=time.monotonic() + 1.0, cancelled=None, kind=None
    )
    assert value == "value"
