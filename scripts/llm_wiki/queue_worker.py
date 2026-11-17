"""The queue worker: claiming tasks, running processors and settling their outcomes."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, Protocol

from llm_wiki.queue_child_runtime import encode_processor_outcome, send_processor_frame
from llm_wiki.queue_child_runtime import run_processor_child as _run_child_runtime
from llm_wiki.queue_contracts import (
    CompileCompletionError,
    DeferredResult,
    LeaseFenceError,
    LeaseProcessor,
    QueueFailure,
    QueueLease,
    QueueOperationError,
    QueueTask,
    ResultConflictError,
)
from llm_wiki.queue_heartbeat import LeaseHeartbeat
from llm_wiki.queue_worker_state import ProcessorOutcome, WorkerProgress, processor_error_code
from secret_redact import redact_secrets


class WorkerQueue(Protocol):
    """The queue operations a worker pass calls; `queue_core.MemoryQueue` provides them."""

    def claim(
        self, owner: str, *, lease_seconds: int = ..., max_attempts: int | None = None
    ) -> QueueLease | None: ...

    def get(self, task_id: str) -> QueueTask: ...

    def fail(self, lease: QueueLease, failure: QueueFailure, *, max_attempts: int | None = None,
             retry_base_seconds: int | None = None, retry_cap_seconds: int | None = None) -> None: ...

    def adopt_published_result(self, lease: QueueLease, *, operation_id: str) -> str | None: ...

    def publish_result(self, lease: QueueLease, *, operation_id: str, result: bytes) -> str: ...

    def acknowledge(self, lease: QueueLease) -> QueueTask: ...


# One trail line per failed processor. The trail is read by a person at
# session start, so the reason has to fit a line, not carry a stack.
MAX_PROCESSOR_REASON_CHARS = 300


def compat_task(task: QueueTask | QueueLease) -> dict[str, Any]:
    created = task.created_at
    last_attempt = task.last_attempt_at
    attempts = task.attempts if isinstance(task, QueueTask) else task.prior_attempts
    return {
        "id": task.id,
        "type": task.kind,
        "handler_version": task.handler_version,
        "enqueued_at": created.isoformat(timespec="seconds"),
        "attempts": attempts,
        "last_attempt_at": (
            last_attempt.isoformat(timespec="seconds") if last_attempt else None
        ),
        "payload": task.payload,
    }


def run_processor_inline(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    del timeout
    return processor(task)


def _record_processor_failure(task: dict[str, Any], error: BaseException) -> None:
    """Leave the reason on disk before the wire flattens it to one letter.

    The frame deliberately carries a stable code and no traceback, so the
    parent can only ever record `processor_failed`. Measured 2026-08-28 on the
    live vault: 72 such attempts and eleven `flush` tasks stuck at eight
    attempts each, with no reason anywhere — not in `attempt_history`, not in
    the step's stderr. Eleven sessions had failed to become memory and the
    trail could not say why. Recording here keeps the wire contract and still
    answers the question.
    """
    try:
        from capture_diagnostics import record_capture_failure

        kind = str(task.get("kind", "unknown"))
        reason = f"{kind}: {type(error).__name__}: {redact_secrets(str(error))}"
        record_capture_failure(
            "queue_processor", reason[:MAX_PROCESSOR_REASON_CHARS],
            error=error,
            session_id=str(task.get("id", ""))[:32] or None,
        )
    except Exception:  # noqa: BLE001 - diagnostics never change the outcome
        pass


def processor_result_frame(
    processor: Callable[[dict], bool | DeferredResult], task: dict[str, Any]
) -> bytes:
    """Run the task; a failure is reported as a stable code, never a traceback."""
    try:
        return encode_processor_outcome(processor(task))
    except Exception as error:  # noqa: BLE001 - parent receives a stable code only
        _record_processor_failure(task, error)
        return b"E"


def processor_child_entry(
    sender: Any,
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
) -> None:
    try:
        # Results use sender, never stdout (which may carry the parent's MCP).
        os.dup2(2, 1)
        if os.name != "nt":
            os.setsid()
        send_processor_frame(sender, processor_result_frame(processor, task))
    except Exception:  # noqa: BLE001 - parent receives a stable code only
        pass
    finally:
        sender.close()


def run_processor_child(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
    lease_lost: Callable[[], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool | DeferredResult:
    return _run_child_runtime(
        processor, task, timeout, lease_lost, cancelled,
        child_entry=processor_child_entry,
    )


# The pause between claim attempts while the queue database is busy, within the
# caller's deadline. Basis unknown: value predates measurement; review if claims
# show contention in doctor or the nightly log.
_CLAIM_BUSY_RETRY_SECONDS = 0.05


def is_busy_database(error: sqlite3.OperationalError) -> bool:
    from llm_wiki.sqlite_deadlines import busy_database

    return busy_database(error)


def _worker_claim_options(lease_seconds, max_attempts, task_id):
    options = {'lease_seconds': lease_seconds, 'max_attempts': max_attempts}
    if task_id is not None:
        options['task_id'] = task_id
    return options


def _worker_claim_active(deadline, monotonic, cancelled):
    return monotonic() < deadline and not (cancelled is not None and cancelled())


def claim_when_reachable(
    queue: WorkerQueue,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
    deadline: float,
    monotonic: Callable[[], float],
    task_id: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> QueueLease | None:
    """A busy database means "try again shortly", not a dead worker.

    Another worker holding the write lock used to surface as an unhandled
    `database is locked` out of the worker loop, which ended the run.
    """
    options = _worker_claim_options(lease_seconds, max_attempts, task_id)
    while _worker_claim_active(deadline, monotonic, cancelled):
        lease, reached = _claim_unless_busy(queue, owner, options)
        if reached:
            return lease
        time.sleep(max(0.0, min(_CLAIM_BUSY_RETRY_SECONDS, deadline - monotonic())))
    return None


def _claim_unless_busy(
    queue: WorkerQueue, owner: str, options: dict
) -> tuple[QueueLease | None, bool]:
    """The claim and True, or None and False when the database was busy."""
    try:
        return queue.claim(owner, **options), True
    except sqlite3.OperationalError as error:
        # Only a busy database is "nothing to claim yet". A disk I/O error or
        # a missing table is a failure the operator has to see, not an idle queue.
        if not is_busy_database(error):
            raise
    return None, False


def _adopted_counts(
    queue: WorkerQueue, lease: QueueLease, counts: dict[str, int]
) -> bool:
    """True when a previously published result settled this lease already."""
    adopted = queue.adopt_published_result(lease, operation_id=lease.id)
    if adopted == "adopted":
        _count_terminal(counts, queue.acknowledge(lease))
        return True
    if adopted == "corrupt":
        _count_terminal(counts, queue.get(lease.id))
        return True
    return False


def run_processor(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    remaining: float,
) -> ProcessorOutcome:
    try:
        outcome = processor_runner(processor, compat_task(lease), remaining)
    except TimeoutError:
        return ProcessorOutcome(False, True, False)
    except QueueOperationError as exc:
        return ProcessorOutcome(False, False, exc.code == "process_cleanup_failed", exc.code)
    except Exception as exc:  # noqa: BLE001 - queue exposes stable codes only
        return ProcessorOutcome(False, False, False, processor_error_code(exc))
    return ProcessorOutcome(outcome, False, False)


def interruptible(processor_runner, heartbeat: LeaseHeartbeat, *, cancelled=None):
    """The child runner stops its child when the lease is lost; injected runners stay as they are."""
    if processor_runner is not run_processor_child:
        return processor_runner
    return partial(run_processor_child, lease_lost=lambda: heartbeat.error is not None,
                   cancelled=cancelled)


def _processor_result(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    heartbeat: LeaseHeartbeat,
    deadline: float,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None = None,
) -> ProcessorOutcome:
    heartbeat.start()
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return ProcessorOutcome(False, True, False)
        result = run_processor(processor, interruptible(processor_runner, heartbeat, cancelled=cancelled), lease, remaining)
        if monotonic() >= deadline:
            return ProcessorOutcome(result.outcome, True, result.cleanup_failed, result.error_code)
        return result
    finally:
        heartbeat.stop()


def _fail_lease(
    queue: WorkerQueue,
    lease: QueueLease,
    failure: QueueFailure,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    queue.fail(
        lease,
        failure,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _count_terminal(counts, queue.get(lease.id))


def unsuccessful_worker_failure(result: ProcessorOutcome) -> QueueFailure | None:
    if result.timed_out:
        return QueueFailure("worker_timeout")
    if not bool(result.outcome):
        return QueueFailure(result.error_code or "processor_failed")
    return None


def _worker_failure(result: ProcessorOutcome) -> QueueFailure | None:
    """The failure this outcome means, or None when the task succeeded."""
    if result.cleanup_failed:
        return QueueFailure(
            "process_cleanup_failed", blocked_capability="process_cleanup"
        )
    if result.error_code == 'worker_cancelled':
        return QueueFailure('worker_cancelled', blocked_capability='caller_cancelled')
    return unsuccessful_worker_failure(result)


def _settle_worker_failure(
    queue: WorkerQueue,
    lease: QueueLease,
    result: ProcessorOutcome,
    failure: QueueFailure,
    counts: dict[str, int],
    bounds: dict[str, int],
) -> None:
    """Count a halted cleanup, then settle the lease as failed."""
    if result.cleanup_failed:
        counts["halted"] = 1
    _fail_lease(queue, lease, failure, counts, **bounds)


def _published_result_data(outcome: bool | DeferredResult) -> bytes:
    return outcome.data if isinstance(outcome, DeferredResult) else b""


def _settle_processor_outcome(
    queue: WorkerQueue,
    lease: QueueLease,
    result: ProcessorOutcome,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    bounds = {
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    failure = _worker_failure(result)
    if failure is not None:
        _settle_worker_failure(queue, lease, result, failure, counts, bounds)
        return
    _settle_successful_processor(queue, lease, result, counts, bounds)


def _settle_successful_processor(queue, lease, result, counts, bounds):
    try:
        queue.publish_result(
            lease, operation_id=lease.id, result=_published_result_data(result.outcome),
        )
        task = queue.acknowledge(lease)
    except CompileCompletionError as error:
        failure = QueueFailure(error.code, blocked_capability='compile_completion')
        _fail_lease(queue, lease, failure, counts, **bounds)
        return
    _count_terminal(counts, task)


def _heartbeat_lost(heartbeat: LeaseHeartbeat, result: ProcessorOutcome) -> bool:
    """Whether the lease heartbeat died without the cleanup already failing."""
    return heartbeat.error is not None and not result.cleanup_failed


def _count_dead_task(queue: WorkerQueue, lease: QueueLease, counts: dict[str, int]) -> None:
    """A task the queue has already buried is counted dead as well as failed."""
    if queue.get(lease.id).state == "dead":
        counts["dead"] += 1


def _settle_worker_outcome(
    queue: WorkerQueue,
    lease: QueueLease,
    result: ProcessorOutcome,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    """Settle the attempt; a lost fence counts as a failure, never a crash."""
    try:
        _settle_processor_outcome(
            queue,
            lease,
            result,
            counts,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
    except (LeaseFenceError, ResultConflictError):
        counts["failed"] += 1
        _count_dead_task(queue, lease, counts)


def work_one(
    queue: WorkerQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    *,
    owner: str,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    task_id: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    lease = claim_when_reachable(
        queue,
        owner=owner,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        deadline=deadline,
        monotonic=monotonic,
        task_id=task_id,
        cancelled=cancelled,
    )
    return work_claimed(queue, processor, processor_runner, lease,
        deadline=deadline, monotonic=monotonic, lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, retry_cap_seconds=retry_cap_seconds,
        cancelled=cancelled)


def work_claimed(queue, processor, processor_runner, lease, *, deadline, monotonic,
                  lease_seconds, heartbeat_seconds, max_attempts, retry_base_seconds,
                  retry_cap_seconds, cancelled=None):
    """Supervise and settle a lease issued by the same canonical claim path."""
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0, "halted": 0}
    if lease is None:
        return counts
    if _adopted_counts(queue, lease, counts):
        return counts
    run_and_settle(
        queue,
        processor,
        processor_runner,
        lease,
        counts,
        deadline=deadline,
        monotonic=monotonic,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
        cancelled=cancelled,
    )
    return counts


def bind_lease_processor(processor, queue, lease, deadline):
    if isinstance(processor, LeaseProcessor):
        return partial(processor.process, queue, lease, deadline)
    return processor


def run_and_settle(
    queue: WorkerQueue,
    processor: Callable[[dict], bool | DeferredResult] | LeaseProcessor,
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    counts: dict[str, int],
    *,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Run one claimed task under a heartbeat and settle whatever came back."""
    processor = bind_lease_processor(processor, queue, lease, deadline)
    heartbeat = LeaseHeartbeat(
        queue,
        lease,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    result = _processor_result(
        processor, processor_runner, lease, heartbeat, deadline, monotonic, cancelled=cancelled
    )
    if _heartbeat_lost(heartbeat, result):
        counts["failed"] += 1
        return
    _settle_worker_outcome(
        queue,
        lease,
        result,
        counts,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )


# A worker claims no task with less than this left: one provider call (90 s by default)
# plus the child's start and settle. A task claimed later is killed by the worker's own
# deadline and loses an attempt. See
# `docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
WORKER_MIN_CLAIM_SECONDS = 120


def _worker_exhausted(
    *,
    cancelled: Callable[[], bool] | None,
    now: float,
    deadline: float,
    idle_started: float | None,
    idle_seconds: int,
) -> bool:
    """The worker has run out of time, permission, or patience."""
    if cancelled is not None and cancelled():
        return True
    if now >= deadline:
        return True
    return idle_started is not None and now - idle_started >= idle_seconds


def _worker_idle_pause(
    progress: WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Sleep out part of the idle window; False when the budget is gone."""
    idle_remaining = idle_seconds - (now - progress.idle_started)
    budget_remaining = deadline - monotonic()
    if budget_remaining <= 0:
        return False
    sleep(min(1.0, idle_remaining, budget_remaining))
    return True


def _worker_idle_step(
    progress: WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Wait out one idle turn; False when the worker should stop."""
    if idle_seconds == 0:
        return False
    if progress.idle_started is None:
        progress.idle_started = now
    return _worker_idle_pause(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def _worker_turn(
    queue: WorkerQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: WorkerProgress,
    *,
    now: float,
    owner: str,
    deadline: float,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    policy: Mapping[str, int],
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """One turn of the worker loop; False when it should stop."""
    counts = work_one(
        queue,
        processor,
        processor_runner,
        owner=owner,
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
        **policy,
    )
    outcome = progress.advance(counts)
    if outcome == "halted":
        return False
    if outcome == "worked":
        return True
    return _worker_idle_step(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def drive_worker(
    queue: WorkerQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: WorkerProgress,
    *,
    owner: str,
    deadline: float,
    max_tasks: int,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    cancelled: Callable[[], bool] | None,
    policy: Mapping[str, int],
    min_claim_seconds: float = 0,
) -> None:
    while progress.processed < max_tasks:
        now = monotonic()
        if _worker_exhausted(
            cancelled=cancelled,
            now=now,
            deadline=deadline - min_claim_seconds,
            idle_started=progress.idle_started,
            idle_seconds=idle_seconds,
        ):
            return
        if not _worker_turn(
            queue,
            processor,
            processor_runner,
            progress,
            now=now,
            owner=owner,
            deadline=deadline,
            idle_seconds=idle_seconds,
            monotonic=monotonic,
            sleep=sleep,
            policy=policy,
            cancelled=cancelled,
        ):
            return


def _count_terminal(counts: dict[str, int], task: QueueTask) -> None:
    if task.state == "succeeded":
        counts["ok"] += 1
        return
    counts["failed"] += 1
    if task.state == "dead":
        counts["dead"] += 1
