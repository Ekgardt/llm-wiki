"""Work that finished is kept, even when the wall deadline passed while it landed.

The worker runs each handler in a child process with a deadline. It used to
raise `TimeoutError` the moment the deadline passed — without asking whether the
child's answer had arrived since the last look, and again while waiting for a
child that had already sent its answer to exit. Either way the frame was
dropped, the task was failed as `worker_timeout`, and a paid-for `query` answer
was lost.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


class _PipeThatMissesTheFirstLook:
    """A pipe whose first look finds nothing: the answer lands right after it.

    This is the race the fix is about — `poll` expires, the child writes its
    frame, and only then does the parent test its deadline.
    """

    def __init__(self, receiver) -> None:
        self._receiver = receiver
        self._looked = False

    def poll(self, timeout: float = 0.0) -> bool:
        if not self._looked:
            self._looked = True
            return False
        return self._receiver.poll(timeout)

    def recv_bytes(self, maxlength: int) -> bytes:
        return self._receiver.recv_bytes(maxlength)


def _sleep_forever() -> None:
    time.sleep(3600)


def _expired_run(process: multiprocessing.Process, receiver) -> memory_queue._ChildRun:
    """A child whose deadline is already in the past."""
    return memory_queue._ChildRun(
        process, receiver, deadline=time.monotonic() - 1.0, tracked_descendants=set()
    )


def _finished_process() -> multiprocessing.Process:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=len, args=((),))
    process.start()
    process.join(SHORT_TIMEOUT)
    return process


def test_an_answer_that_lands_after_the_last_look_ends_the_wait() -> None:
    receiver, sender = multiprocessing.Pipe(duplex=True)
    sender.send_bytes(b"T")
    process = _finished_process()
    run = _expired_run(process, _PipeThatMissesTheFirstLook(receiver))

    memory_queue._await_child_message(run)

    assert (run.receiver.poll(0), process.exitcode) == (True, 0)


def test_an_empty_pipe_at_the_deadline_is_still_a_timeout() -> None:
    receiver, _sender = multiprocessing.Pipe(duplex=True)
    process = _finished_process()
    run = _expired_run(process, receiver)

    with pytest.raises(TimeoutError):
        memory_queue._await_child_message(run)


def test_a_child_that_lingers_after_answering_does_not_lose_its_answer() -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_sleep_forever, daemon=False)
    process.start()
    receiver, _sender = multiprocessing.Pipe(duplex=True)
    run = _expired_run(process, receiver)

    try:
        memory_queue._settle_child_exit(run)
    finally:
        process.terminate()
        process.join(SHORT_TIMEOUT)

    # The lingering child was stopped rather than allowed to fail the task.
    assert process.is_alive() is False
