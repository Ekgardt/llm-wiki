"""Queue child lifecycle and bounded byte protocol, independent of the executor."""
from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from llm_wiki.queue_contracts import MAX_RESULT_BYTES, DeferredResult, QueueOperationError
from llm_wiki.queue_process_cleanup import terminate_processor_child, tracked_descendant_pids


def encode_processor_outcome(outcome: object) -> bytes:
    """The wire frame that carries one processor outcome to the parent."""
    if isinstance(outcome, DeferredResult):
        return b"D" + outcome.data
    if isinstance(outcome, bool):
        return b"T" if outcome else b"F"
    return b"?"


def send_processor_frame(sender: Any, frame: bytes) -> None:
    """Hand the frame over, but only once the parent says it is listening.

    `frame` is the finished result: the caller evaluates it before this runs, so
    `R` means "the task is done and I have an answer", not "I am up". The parent
    reads the child's descendants on this signal, and that ordering is the
    reason the set it gets is the tree the task left running rather than an
    empty one. See
    `docs/research/2026-09-18-a-childs-tree-is-read-when-it-is-killed.md`.
    """
    sender.send_bytes(b"R")
    if not sender.poll(5) or sender.recv_bytes(1) != b"A":
        return
    sender.send_bytes(frame)


_PROCESSOR_BOOLEAN_FRAMES = MappingProxyType({b"T": True, b"F": False})


def _decode_deferred_frame(frame: bytes) -> DeferredResult:
    """The deferred result a `D` frame carries."""
    data = frame[1:]
    if len(data) > MAX_RESULT_BYTES:
        raise QueueOperationError("processor_result_oversize")
    return DeferredResult(data)


def _processor_frame_failure(frame: bytes) -> str:
    """Which failure a frame that carries no outcome means."""
    return "processor_exception" if frame == b"E" else "processor_result_malformed"


def decode_processor_frame(frame: bytes) -> bool | DeferredResult:
    if frame[:1] == b"D":
        return _decode_deferred_frame(frame)
    if frame in _PROCESSOR_BOOLEAN_FRAMES:
        return _PROCESSOR_BOOLEAN_FRAMES[frame]
    raise QueueOperationError(_processor_frame_failure(frame))


@dataclass
class ChildRun:
    """One processor child: its process, its pipe, and its deadline."""

    process: multiprocessing.Process
    receiver: object
    deadline: float
    tracked_descendants: set[int] | None = None
    lease_lost: Callable[[], bool] | None = None
    cancelled: Callable[[], bool] | None = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def lost(self) -> bool:
        return self.lease_lost is not None and self.lease_lost()

    def stop(self) -> None:
        """Terminate the child, discovering its tree when we never tracked it.

        Passing an explicit None means "the descendants are unknown"; only the
        default sentinel asks for discovery, and before the handshake that is
        what we want.
        """
        tracked = self.tracked_descendants
        if tracked is None:
            self.tracked_descendants = terminate_processor_child(self.process)
            return
        self.tracked_descendants = terminate_processor_child(
            self.process, tracked_descendants=tracked
        )


# How often a waiting parent looks at its lease. See
# `docs/research/2026-09-14-a-lost-lease-stops-its-child.md`.
CHILD_LEASE_CHECK_SECONDS = 1.0


def await_child_message(run: ChildRun) -> None:
    """Wait for the child to say something before its deadline runs out or its lease is lost."""
    while not run.receiver.poll(max(0.0, min(run.remaining(), CHILD_LEASE_CHECK_SECONDS))):
        _stop_child_if_done_waiting(run)


def _stop_child_if_done_waiting(run: ChildRun) -> None:
    """A deadline decides whether to keep waiting, not whether to drop an answer.

    Without the second look, a frame written between the poll's expiry and this
    check became `worker_timeout` and the finished work was thrown away. See
    `docs/research/2026-09-18-an-answer-that-arrived-is-not-a-timeout.md`.
    """
    reason = _child_stop_reason(run)
    if reason is not None:
        run.stop()
        raise QueueOperationError(reason)
    if _may_keep_waiting(run):
        return
    run.stop()
    raise TimeoutError


def _child_stop_reason(run: ChildRun) -> str | None:
    if run.lost():
        return 'lease_lost'
    if run.cancelled is not None and run.cancelled():
        return 'worker_cancelled'
    return None


def _may_keep_waiting(run: ChildRun) -> bool:
    """Time left, or an answer already in the pipe; either ends this check."""
    return run.remaining() > 0 or bool(run.receiver.poll(0))


def child_ready_handshake(run: ChildRun) -> None:
    """The child says it has an answer, and only then do we track its tree.

    `R` is sent after the processor has returned, not when the child starts, so
    the tree recorded here is the one the finished task left behind — a provider
    the processor shelled out to and did not wait for. Moving this earlier would
    record an empty set and leave `_cleanup_confirmed` and `_await_cleanup`
    verifying nothing.
    """
    await_child_message(run)
    try:
        ready = run.receiver.recv_bytes(1)
    except (OSError, EOFError):
        raise QueueOperationError("processor_result_malformed") from None
    if ready != b"R":
        raise QueueOperationError("processor_result_malformed")
    run.tracked_descendants = tracked_descendant_pids(run.process.pid, os.name)
    run.receiver.send_bytes(b"A")


def _abandon_oversize_child(run: ChildRun) -> None:
    """A child that sent more than we accept is stopped, not waited out."""
    run.receiver.close()
    run.process.join(0.5)
    if run.process.is_alive():
        run.stop()


def _child_result_frame(run: ChildRun) -> bytes:
    """The result frame the child sent, or why it could not be read."""
    await_child_message(run)
    try:
        return run.receiver.recv_bytes(MAX_RESULT_BYTES + 1)
    except OSError:
        _abandon_oversize_child(run)
        raise QueueOperationError("processor_result_oversize") from None
    except EOFError:
        raise QueueOperationError("processor_result_malformed") from None


def _child_left_in_time(run: ChildRun) -> bool:
    """Whether the child exited within its deadline; a lingering one is stopped."""
    while run.process.is_alive() and run.remaining() > 0:
        if _child_stop_reason(run) is not None:
            break
        run.process.join(min(run.remaining(), CHILD_LEASE_CHECK_SECONDS))
    if not run.process.is_alive():
        return True
    run.stop()
    return False


def settle_child_exit(run: ChildRun) -> None:
    """Check how the child left, once its answer is already in hand.

    A child that will not leave is a cleanup problem — `stop` kills its tree —
    and no longer a reason to discard the frame it already sent. One that does
    leave is still judged by its exit code. See
    `docs/research/2026-09-18-an-answer-that-arrived-is-not-a-timeout.md`.
    """
    if not _child_left_in_time(run):
        return
    if run.process.exitcode != 0:
        raise QueueOperationError("processor_child_failed")


def _require_child_start(timeout, cancelled):
    if timeout <= 0:
        raise TimeoutError
    if cancelled is not None and cancelled():
        raise QueueOperationError('worker_cancelled')


def run_processor_child(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
    lease_lost: Callable[[], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
    *,
    child_entry: Callable[[Any, Callable[[dict], bool | DeferredResult], dict[str, Any]], None],
) -> bool | DeferredResult:
    _require_child_start(timeout, cancelled)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(
        target=child_entry,
        args=(sender, processor, task),
        daemon=False,
    )
    run = ChildRun(process, receiver, time.monotonic() + timeout,
                    lease_lost=lease_lost, cancelled=cancelled)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        child_ready_handshake(run)
        frame = _child_result_frame(run)
        settle_child_exit(run)
        return decode_processor_frame(frame)
    finally:
        receiver.close()
        if started:
            # Through `stop`, which knows that a tree never tracked — a child
            # that died before its handshake — is discovered now, not unknown.
            run.stop()
