# An answer that arrived is not a timeout

Dated 2026-09-18. Finding Q-L21 of the third audit, and the neighbour of the same shape one
step later in the same function.

Files: scripts/memory_queue.py,
tests/test_an_answer_that_landed_at_the_deadline_is_kept.py

## What was found

The worker runs each handler in a child process with a wall deadline. Two places throw away
work that is already finished:

- `_stop_child_if_done_waiting` is reached when `receiver.poll(...)` returned False. It raises
  `TimeoutError` as soon as `remaining() <= 0`, without asking again whether anything arrived.
  The child can finish and write its frame in the window between the poll's expiry and that
  check, so a complete result becomes `worker_timeout` and the task is failed and retried.
- `_join_child` then waits for the child to *exit* within the same deadline, after
  `_child_result_frame` has already read the frame. A child that sent its answer and lingers
  (a slow interpreter shutdown, an atexit handler, a stuck descendant) makes the parent raise
  `TimeoutError` and discard the frame it is holding. The kill sequence in `run.stop()` handles
  the lingering child perfectly well; only the result is lost.
- The cost is not just a retry. `_manual_query_task` results are published by the queue as the
  answer to a `query`; a lost frame means the model call is paid for again, and the audit's
  own note on this finding is "a `query` result is lost".

## Practice on this date

- This is the standard cancellation rule: a deadline decides whether to *keep waiting*, not
  whether to discard an answer already in hand. Go's `context` documentation states the
  separation directly — "Canceling this context releases resources associated with it, so code
  should call cancel as soon as the operations running in this Context complete"
  ([Go, context package](https://pkg.go.dev/context)) — cancellation frees the worker, it does
  not invalidate a value already returned.
- The same reading applies to the receive side: a `poll()` that returned False a moment ago is
  evidence about that moment, not about now. Python's `multiprocessing.connection.Connection.poll`
  "Return whether there is any data available to be read" with an optional timeout
  ([Python, multiprocessing](https://docs.python.org/3/library/multiprocessing.html)); asking
  again costs one syscall.

## The decision

- Before a wait turns into a timeout, the parent polls once more with no wait. A frame that is
  already there ends the wait normally, and the caller reads it.
- Once the result frame is in hand, a child that will not exit within the deadline is stopped
  and its non-exit is no longer fatal: the work is done. A child that *does* exit still has its
  exit code checked, so a crash after sending a frame stays a failure.
- A lost lease keeps raising as it did: without the fence the result cannot be published, so
  there is nothing to keep.
