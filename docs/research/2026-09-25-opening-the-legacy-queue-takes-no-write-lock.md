# Opening the legacy queue takes no write lock

Date: 2026-09-25. Q8 guard against the Windows CI flake of run 36182427739.

## Fact
- `MemoryQueue.__init__` (the pre-adoption queue, still used on a vault that has
  not adopted Reliability v3 and by the tests) opens `BEGIN IMMEDIATE` on every
  construction to run `_retire_exhausted_ready`, whether or not any ready task is
  out of attempts.
- CI run 36182427739 (windows, py3.11, shard 3) failed
  `test_archive_winning_finalization_race_deletes_before_failure_records` with
  `database is locked` raised from that construction, while the archive held the
  queue's write lock by design.

## Source (fetched 2026-09-25)
SQLite, "BEGIN TRANSACTION", https://www.sqlite.org/lang_transaction.html:
"IMMEDIATE causes the database connection to start a new write immediately,
without waiting for a write statement. The BEGIN IMMEDIATE might fail with
SQLITE_BUSY if another write transaction is already active on another database
connection."

## Decision
Construction first reads whether any ready task has exhausted its attempts, and
takes the write lock only when one has. The retirement itself is unchanged (it
still runs under `BEGIN IMMEDIATE`, and the claim path retires on its own as
before). Constructing a queue that has nothing to retire no longer competes for
the write lock.

## Uncertainty
The flake was seen once; that it came from this construction is from its
traceback, not from a reproduction. The second flake of that run (access tracking
cursor wrap, 150 s deadline on Windows) is a timing budget, not this.

## Files
- scripts/memory_queue.py
- tests/test_opening_the_legacy_queue_takes_no_write_lock.py
