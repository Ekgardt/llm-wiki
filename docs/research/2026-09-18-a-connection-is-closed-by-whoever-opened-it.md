# A connection is closed by whoever opened it

Dated 2026-09-18. Finding Q-L18 of the third audit.

Files: scripts/memory_queue.py, scripts/repair_exhausted_queue_tasks.py,
tests/test_every_queue_ownership_handle_is_closed.py

## What was found

- The four callers of `_open_queue_ownership_db` write
  `with _open_queue_ownership_db(...) as connection`. That is `sqlite3.Connection`'s own context
  manager, which commits or rolls back and **does not close**. The handle then lives until the
  connection object is deallocated.
- **The audit's evidence for this one is wrong, and the severity with it.** It reports
  "`ResourceWarning` ×3 under `-X dev`" and a Windows file kept in use. Measured here on
  CPython 3.12 with `-X dev`: an unclosed `sqlite3` connection emits no `ResourceWarning` at
  all, and the descriptor is gone as soon as the last reference to the connection goes — which,
  under reference counting, is the end of the same function. So in ordinary operation no handle
  outlives the call, and the Windows consequence does not follow.
- What remains is real but smaller: the close is left to deallocation rather than stated. A
  reference cycle, a kept traceback, or another implementation's collector moves it to an
  unknown later moment, and this is the one place in the module that relies on that — every
  other caller of a bare connection here already writes `closing(...)`.
- `repair_exhausted_queue_tasks.py` has the same shape one level up. `MemoryQueue._connect` is a
  `@contextmanager` that closes its connection, while `_QueueV3CandidateReader._connect` returns
  a bare connection — inside `memory_queue.py` every caller of the latter wraps it in
  `closing(...)`, and this script is the one place that does not. So on an adopted vault the
  repair leaves a handle open; on a pre-adoption vault it does not.
  This also means the obvious one-line fix is wrong: `closing(queue._connect())` would bind the
  *context manager object* on the legacy path and then fail for having no `close`.

## Practice on this date

- Python states the distinction the code is missing: "Connection object used as context manager
  only commits or rollbacks transactions, so the connection object should be closed manually"
  ([Python, sqlite3](https://docs.python.org/3/library/sqlite3.html)).
- The standard tool for the other half is `contextlib.closing`, whose documented purpose is
  exactly this: "Return a context manager that closes *thing* upon completion of the block"
  ([Python, contextlib](https://docs.python.org/3/library/contextlib.html)).

## The decision

- The four ownership-registry callers wrap the opener in `closing(...)`, keeping
  `begin_immediate` inside it so the transaction still settles before the handle goes.
- The repair script asks what it was handed: a bare connection is wrapped in `closing`, a context
  manager is used as it is. One `with` serves both queues and each closes the handle it opened.
- No behaviour changes beyond the handle's lifetime: reads need no commit, and the one write
  already runs inside `begin_immediate`, which commits itself.
- The test that ships with this change guards what the change could break — that acquiring,
  renewing and releasing an owner still writes and clears its row through the now-closed
  handle. No test asserts the absence of a leak, because on CPython there is no observable
  difference to assert: claiming one would be a test that passes for the wrong reason.
