"""Foreground priority, optional-stage admission, observed costs and bounded optional workers."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# Abandoned optional stages allowed to run on at once (docs/research/2026-08-26-who-pays-for-an-
# abandoned-optional-stage.md); bounds threads a slow lane can hold. Basis unknown: value predates
# measurement; review when stages are refused for lack of a slot.
MAX_OPTIONAL_STRAGGLERS = 2

# The ceiling on one optional stage, on top of the share of the caller's budget
# that `optional_stage_deadline` already grants it. It exists for the caller who
# passes a very generous deadline: half of ten minutes is still five minutes to
# wait on a signal nobody needs.
#
# It arrived as a bare 0.5 with no measurement and silently overrode the share
# for everyone. Measured on this vault: the warm dense stage of one recall costs
# 0.99-1.33 s and the cold one — the first in a process, which now carries the
# model load — costs 8.85 s. Every one of those is above 0.5, so no caller that
# passed a deadline could ever use the semantic leg: six recall calls in one
# server, all six `optional_stage_timeout`, lexical only.
#
# 12 s is above the measured cold stage with room for a loaded machine, so a
# caller that granted enough budget can pay the one-time load on its only call —
# which is what a one-shot CLI answer needs, having no second call to be warm
# for. Short-budget callers are unaffected by the ceiling because the share
# binds first: the MCP path's ten seconds still grant an optional stage five.
# What it costs: a stage that will never finish now delays an answer by up to
# 12 s instead of 0.5 s, and only for a caller that granted at least 24 s.
OPTIONAL_STAGE_MAX_SECONDS = 12.0
OPTIONAL_STAGE_SLOTS = threading.BoundedSemaphore(MAX_OPTIONAL_STRAGGLERS)

# One straggler slot per kind of optional stage, instead of one shared pool of
# `MAX_OPTIONAL_STRAGGLERS` that every kind competes for.
#
# The shared pool made a straggler of one kind refuse admission to a stage of
# another kind, before that stage waited for anything. Measured on the live
# vault, six recall calls in one process at the 10 s MCP budget: call 1
# abandoned both of its stages, they held both slots for the length of their
# work, and calls 2, 4 and 6 were refused `optional stage capacity exhausted`
# at 0.00 s -- the dense leg reached the answer in one call out of six, 1.75 of
# six averaged over four such rounds. The two hogs are model loads: about 9 s
# for the embedding model, about 20 s for the cross-encoder, both far longer
# than the 5 s an MCP-budget stage may wait, so one abandoned rerank load shut
# the dense leg out of every call behind it. Partitioned, the same measurement
# gives 3.5 of six; see
# `docs/research/2026-08-26-who-pays-for-an-abandoned-optional-stage.md`.
#
# Partitioning by kind is the bulkhead rule: capacity for one dependency is
# reserved from capacity for all the others, so a slow one exhausts only its
# own. It also makes each kind single-flight, which is stricter than the shared
# pool was -- two dense stragglers could previously load the embedding model
# twice at about 1.1 GiB each, a cost `search_memory.lazy_generation_query_encoder`
# accepts in its docstring and this now prevents.
#
# What it costs. A second stage of a kind already in flight is refused
# immediately rather than queued behind it: the caller could instead wait for
# the straggler and then run warm, but the measured loads (9 s, 20 s) do not
# fit in an MCP-budget stage (5 s), so that wait would spend the caller's
# budget and delay the lexical answer for a leg that still could not finish.
# The live thread bound is unchanged at two, one per kind, and the kinds are a
# fixed tuple rather than caller-supplied, so no call can widen it.
OPTIONAL_STAGE_KINDS = ("dense", "rerank")
OPTIONAL_STAGE_KIND_SLOTS = {
    kind: threading.BoundedSemaphore(1) for kind in OPTIONAL_STAGE_KINDS
}


def optional_stage_slots(kind: str | None) -> threading.BoundedSemaphore:
    """The straggler slots this stage competes for; unlabelled work shares one pool."""
    # Separate measured costs, one cross-encoder and the same concurrency bound.
    if kind == "rerank_events":
        kind = "rerank"
    return OPTIONAL_STAGE_KIND_SLOTS.get(kind, OPTIONAL_STAGE_SLOTS)



# Caller and worker leases outlive fallback answers until their real work settles.
# Condition notifications handle ownership changes; the existing optional-stage
# 10 ms cancellation cadence also observes shutdown while waiting for idle.
_OPTIONAL_WAIT_POLL_SECONDS = 0.01
FOREGROUND_CONDITION = threading.Condition()
FOREGROUND_OWNERS: set[object] = set()
_WARMUP_CANCELLATIONS: set[threading.Event] = set()
BACKGROUND_WORKERS: dict[object, threading.Event] = {}
BACKGROUND_EVENT: ContextVar[threading.Event | None] = ContextVar("retrieval_background_event", default=None)
_BACKGROUND_CANCELLATION: ContextVar[Callable[[], bool] | None] = ContextVar(
    "retrieval_background_cancellation", default=None
)


def _begin_foreground() -> object:
    owner = object()
    with FOREGROUND_CONDITION:
        FOREGROUND_OWNERS.add(owner)
        for event in (*_WARMUP_CANCELLATIONS, *BACKGROUND_WORKERS.values()):
            event.set()
    return owner


def _end_foreground(owner: object | None) -> None:
    with FOREGROUND_CONDITION:
        FOREGROUND_OWNERS.discard(owner)
        BACKGROUND_WORKERS.pop(owner, None)
        FOREGROUND_CONDITION.notify_all()


@contextmanager
def foreground_retrieval(source_tool: str):
    """User work preempts warmup; its spawned workers retain their own leases."""
    if BACKGROUND_EVENT.get() is not None:
        yield
        return
    owner = _begin_foreground()
    try:
        yield
    finally:
        _end_foreground(owner)


def _require_priority_available(deadline: float, stopping: Callable[[], bool]) -> None:
    if stopping():
        raise OptionalStageTimeout("retrieval warmup cancelled")
    if time.monotonic() >= deadline:
        raise OptionalStageTimeout("retrieval warmup idle deadline exceeded")


def _wait_foreground_idle(deadline: float, stopping: Callable[[], bool]) -> None:
    while FOREGROUND_OWNERS or BACKGROUND_WORKERS:
        _require_priority_available(deadline, stopping)
        remaining = deadline - time.monotonic()
        FOREGROUND_CONDITION.wait(min(remaining, _OPTIONAL_WAIT_POLL_SECONDS))
    _require_priority_available(deadline, stopping)


@contextmanager
def warmup_priority(deadline: float, stopping: Callable[[], bool]):
    """Wait for actual idle, then lend this warmup one cancellable ownership token."""
    event = threading.Event()
    with FOREGROUND_CONDITION:
        _wait_foreground_idle(deadline, stopping)
        _WARMUP_CANCELLATIONS.add(event)

    def cancelled() -> bool:
        return event.is_set() or stopping() or time.monotonic() >= deadline

    event_token = BACKGROUND_EVENT.set(event)
    token = _BACKGROUND_CANCELLATION.set(cancelled)
    try:
        yield cancelled
    finally:
        _BACKGROUND_CANCELLATION.reset(token)
        BACKGROUND_EVENT.reset(event_token)
        with FOREGROUND_CONDITION:
            _WARMUP_CANCELLATIONS.discard(event)


def background_cancellation() -> Callable[[], bool] | None:
    """Capture the bound token before passing work to a different thread."""
    return _BACKGROUND_CANCELLATION.get()


def _optional_worker_owner() -> object:
    event = BACKGROUND_EVENT.get()
    if event is None:
        return _begin_foreground()
    owner = object()
    with FOREGROUND_CONDITION:
        BACKGROUND_WORKERS[owner] = event
    return owner


def require_optional_not_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise OptionalStageTimeout("optional stage cancelled")


def require_optional_deadline(deadline: float) -> None:
    if deadline <= time.monotonic():
        raise OptionalStageTimeout("optional caller deadline reached before start")


class OptionalStageTimeout(TimeoutError):
    """An optional uninterruptible stage exceeded its isolated budget.

    `reason` is what the trace reports and `partial` whether the answer lost
    part of what it could have had while it waited.
    """

    reason = "optional_stage_timeout"
    partial = True


class OptionalStageNotAdmitted(OptionalStageTimeout):
    """The caller did not wait: the stage's known cost exceeds the window on offer,
    or no slot was free. Nothing timed out and nothing waited was lost, so it is
    reported as what it is (audit 2026-09-27 B-9,
    docs/research/2026-09-27-a-stage-not-waited-for-is-not-a-timeout.md).
    """

    reason = "optional_stage_not_admitted"
    partial = False


# What one kind of optional stage last cost, when a run of it finished --
# whether the caller waited for that run or abandoned it and the daemon
# straggler finished it later. This is the cost model admission uses.
#
# One sample deep on purpose. A mean would remember the cold model load for the
# rest of the process and refuse a leg that has been warm for an hour; the last
# finished run is the only figure that tracks the thing that actually changes,
# which is whether the model is resident. It self-corrects in one call in
# either direction, and a wrongly skipped stage degrades to the lexical answer,
# which is the designed fallback rather than a failure.
OPTIONAL_STAGE_OBSERVED: dict[str, float] = {}
# When each cost was observed (monotonic). A cost that said "does not fit" is
# tried again after this long, as a background trial, so one slow run under load
# does not switch the stage off for the life of the process (audit 2026-09-26
# B-16, docs/research/2026-09-26-a-rerank-is-tried-again.md).
OPTIONAL_STAGE_OBSERVED_AT: dict[str, float] = {}
OPTIONAL_STAGE_REPROBE_SECONDS = 300.0
OPTIONAL_STAGE_OBSERVED_LOCK = threading.Lock()


def observe_optional_stage(kind: str | None, seconds: float) -> None:
    """Record what a finished run of this kind cost, in units the decision can use.

    Clamped at the ceiling, because the figure is only ever compared against a
    window and no window may exceed `OPTIONAL_STAGE_MAX_SECONDS`. Above that
    everything means the same thing -- "longer than any stage is ever allowed
    to wait" -- so keeping more precision than the comparison can spend only
    creates differences that behave identically. It also stops one pathological
    run governing far longer than it should: a stage that hung for thirty
    seconds is recorded as "does not fit", not as thirty seconds, and the next
    run that finishes replaces it either way.
    """
    if kind is None:
        return
    with OPTIONAL_STAGE_OBSERVED_LOCK:
        OPTIONAL_STAGE_OBSERVED[kind] = min(seconds, OPTIONAL_STAGE_MAX_SECONDS)
        OPTIONAL_STAGE_OBSERVED_AT[kind] = time.monotonic()


def observed_cost_is_stale(kind: str) -> bool:
    """Whether the last observed cost is old enough to be tried again."""
    with OPTIONAL_STAGE_OBSERVED_LOCK:
        observed_at = OPTIONAL_STAGE_OBSERVED_AT.get(kind)
    if observed_at is None:
        return False
    return time.monotonic() - observed_at >= OPTIONAL_STAGE_REPROBE_SECONDS


def observed_optional_stage_cost(kind: str | None) -> float | None:
    if kind is None:
        return None
    with OPTIONAL_STAGE_OBSERVED_LOCK:
        return OPTIONAL_STAGE_OBSERVED.get(kind)


# The time between `optional_stage_deadline` granting a window and
# `optional_stage_fits` measuring it: a few function calls and a semaphore.
# It exists so that "was this stage given the whole ceiling?" is not decided by
# clock slop.
_OPTIONAL_STAGE_WINDOW_SLACK_SECONDS = 0.25


def unknown_cost_stage_fits(window: float) -> bool:
    """Whether to wait for a kind nothing has been observed for yet.

    Only when the caller granted enough budget that the ceiling, not its own
    share, is what bounds the stage. That is the case `OPTIONAL_STAGE_MAX_SECONDS`
    already exists for and says so: a one-shot CLI answer has no second call to
    be warm for, so it must be allowed to pay the one-time model load itself.

    A caller on the MCP budget is the other case. Its share is around 3.5 s
    against a measured cold load of 10.13 s, so waiting cannot succeed -- and it
    does not have to, because the worker below has already been started and the
    straggler that finishes it records what it cost. The next call reads that
    and waits for a warm stage that fits. The cost is learned without anyone
    paying for it twice.
    """
    return window >= OPTIONAL_STAGE_MAX_SECONDS - _OPTIONAL_STAGE_WINDOW_SLACK_SECONDS


def optional_stage_fits(kind: str | None, deadline: float) -> bool:
    """Whether a run of this kind is expected to finish in the window on offer.

    Unlabelled work is always admitted: the cost model is per kind, and a stage
    with no kind has nothing to be modelled against.
    """
    if kind is None:
        return True
    window = deadline - time.monotonic()
    observed = observed_optional_stage_cost(kind)
    if observed is None:
        return unknown_cost_stage_fits(window)
    return observed <= window


def _optional_stage_admitted(
    kind: str | None, deadline: float, cancelled: Callable[[], bool] | None
) -> bool:
    """Whether the caller may *wait* for this stage.

    Distinct from whether the stage may run. A stage the caller cannot wait for
    is still worth starting, because the straggler that finishes it leaves the
    model resident and records what it cost, which is what makes the next call
    cheap. Only the wait is refused.
    """
    if cancelled is not None and cancelled():
        return False
    if deadline - time.monotonic() <= 0:
        return False
    return optional_stage_fits(kind, deadline)


def every_value_measures(_value: Any) -> bool:
    """A stage that says nothing about itself is measured by every run it finishes."""
    return True


def _observe_completed_stage(
    kind: str | None, started: float, value: Any, observes: Callable[[Any], bool]
) -> None:
    """Record what the run cost, unless its own result says it did not finish."""
    if not observes(value):
        return
    observe_optional_stage(kind, time.monotonic() - started)


def run_optional_bounded(
    operation: Callable[[], Any],
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
    kind: str | None = None,
    observes: Callable[[Any], bool] = every_value_measures,
) -> Any:
    """Run optional work with a hard wait bound and capped daemon stragglers.

    A live, uncancelled stage may start even when its waiting share has
    expired. Its original caller deadline is checked at the caller boundary;
    cancellation prevents startup and invalidates any late result or cost.

    `observes` reads the value the operation returned and says whether it is
    evidence of a finished run. A stage that gives up on its own deadline
    returns normally, and timing that run would teach the cost model a number
    bounded by the budget rather than by the work. See
    `docs/research/2026-09-17-an-abandoned-stage-is-not-a-measurement.md`.
    """
    # Decided before the worker starts, and deliberately so: this run is about
    # to record its own cost, and a fast one would otherwise overwrite the
    # observation the decision is being made from.
    require_optional_not_cancelled(cancelled)
    admitted = _optional_stage_admitted(kind, deadline, cancelled)
    slots = optional_stage_slots(kind)
    if not slots.acquire(blocking=False):
        raise OptionalStageNotAdmitted("optional stage capacity exhausted")
    completed = threading.Event()
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    _start_optional_worker(operation, result, completed, slots, kind, observes, cancelled)
    _require_admitted_optional_stage(admitted)
    _await_optional_stage(completed, deadline, cancelled)
    ok, value = result.get_nowait()
    if ok:
        return value
    raise value


def _require_admitted_optional_stage(admitted: bool) -> None:
    """Decline the wait for a stage that is not expected to finish in time.

    The worker is already running when this refuses, and that is deliberate:
    the straggler warming the model is the whole point of the daemon design,
    and only the caller's *wait* is being declined. Measured on this vault, a
    cold embedding load costs 10.13 s against the ~3.5 s window a 10 s
    operation can offer, so waiting for it spends a third of the budget to
    arrive at the same lexical answer the caller would have had for nothing.
    """
    if not admitted:
        raise OptionalStageNotAdmitted("optional stage exceeds the budget on offer")


def _start_optional_worker(
    operation: Callable[[], Any],
    result: queue.Queue[tuple[bool, Any]],
    completed: threading.Event,
    slots: threading.BoundedSemaphore,
    kind: str | None = None,
    observes: Callable[[Any], bool] = every_value_measures,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    owner = _optional_worker_owner()

    def run() -> None:
        started = time.monotonic()
        try:
            require_optional_not_cancelled(cancelled)
            value = operation()
            require_optional_not_cancelled(cancelled)
        except Exception as exc:  # noqa: BLE001 - an interrupt propagates
            result.put((False, exc))
        else:
            # Only a run that produced something is a cost observation. A fast
            # failure is not evidence that the work is cheap, and neither is a
            # run the stage abandoned on its own deadline.
            _observe_completed_stage(kind, started, value, observes)
            result.put((True, value))
        finally:
            completed.set()
            slots.release()
            _end_foreground(owner)

    # Registered, so a closing server waits for inference to finish rather than
    # finalizing under it. See `docs/research/2026-09-14-no-model-running-at-exit.md`.
    import inference_threads

    try:
        inference_threads.start(run, name="llm-wiki-optional-retrieval")
    except BaseException:
        slots.release()
        _end_foreground(owner)
        raise


def _await_optional_stage(
    completed: threading.Event,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Wait out one optional stage; a straggler keeps running as a daemon."""
    stage_deadline = min(deadline, time.monotonic() + OPTIONAL_STAGE_MAX_SECONDS)
    while not completed.is_set():
        if cancelled is not None and cancelled():
            raise OptionalStageTimeout("optional stage cancelled")
        wait = stage_deadline - time.monotonic()
        if wait <= 0:
            raise OptionalStageTimeout("optional stage deadline reached")
        completed.wait(min(wait, _OPTIONAL_WAIT_POLL_SECONDS))


# An optional stage may spend this share of what is left of the operation
# budget, never all of it. Spending all of it is how an optional signal turns
# into a failed answer: the mandatory legs and the caller's own fallback are then
# left with nothing. Measured on this vault: a cold embedding model load takes
# about ten seconds against a ten-second MCP budget, and the lexical answer that
# was ready in 1.3 s was lost with it.
OPTIONAL_STAGE_BUDGET_SHARE = 0.5

# The mandatory tail that must survive every optional stage: fusion, ranking,
# rendering the rows, the closing seal re-check, and the caller's own metadata.
# No optional stage may run past `deadline - this`.
#
# The share alone did not protect the tail. A share is taken from what is
# *left*, so two optional stages in sequence take half, then half of the rest --
# three quarters of the remaining budget -- and neither of them owes the tail
# anything. Measured on this vault under load: the dense and rerank stages
# together spent 4.4-5.5 s of a 10 s operation and returned nothing, the
# deadline then fell during the mandatory tail, and the lexical answer that was
# already computed was discarded. 18 of 36 calls across three runs raised
# instead of answering; not one of them returned a degraded answer.
#
# The reserve covers two measured things, not one.
#
# The tail itself: 0.59-0.86 s over eight instrumented calls, load average 5-16.
#
# And the wait's own overshoot. `_await_optional_stage` polls a `threading.Event`
# every 10 ms, which lands within 3 ms of its deadline in isolation and within
# 73 ms against busy pure-Python siblings -- but a stage that is loading a model
# holds the GIL in long native-adjacent stretches, and the waiter cannot check
# its clock until it gets the interpreter back. Instrumented on the real path:
# a stage granted 3.546 s returned after 4.445 s, 0.9 s late. Nothing in the
# waiter can prevent that, so the reserve absorbs it.
#
# 2.5 s is the measured worst tail (0.86 s) plus the measured worst overshoot
# (0.9 s) with margin. A caller with a generous budget is unaffected: the share
# and `OPTIONAL_STAGE_MAX_SECONDS` bind long before the reserve does.
#
# This is the reserve-for-your-own-response-path rule that deadline propagation
# has always carried: what you hand downstream must not be the whole of what you
# have left. See `docs/research/2026-08-29-what-an-optional-stage-may-spend.md`.
OPTIONAL_STAGE_TAIL_RESERVE_SECONDS = 2.5


def optional_stage_deadline(deadline: float) -> float:
    """The slice an optional stage may use before it is abandoned."""
    now = time.monotonic()
    remaining = deadline - now
    if remaining <= 0:
        return deadline
    share = now + remaining * OPTIONAL_STAGE_BUDGET_SHARE
    return min(share, deadline - OPTIONAL_STAGE_TAIL_RESERVE_SECONDS)
