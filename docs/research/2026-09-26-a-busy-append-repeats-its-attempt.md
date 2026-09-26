# A busy append repeats its attempt

Date: 2026-09-26. Audit 2026-09-26 B-27 (the Windows append-race failure).

## Facts

- CI run 36204706220 (Windows) failed
  `tests/test_append_race_lineage.py::test_every_refused_append_is_named_by_the_retry_that_replaced_it`
  with `[OperationalError('database is locked')]`: one of four concurrent writers
  saw its append raise instead of retry. `_append_until_committed` retried a lost
  compare-and-swap, but a busy database inside an attempt, or in the lineage read
  after it (`_refused_parent`), escaped to the caller.
- SQLite result codes (https://www.sqlite.org/rescode.html, fetched 2026-09-26),
  SQLITE_BUSY: "the database file could not be written (or in some cases read)
  because of concurrent activity by some other database connection … Process B
  will need to wait for process A to finish its transaction".

## Decision

- An attempt that fails with transient writer contention
  (`_is_transient_writer_contention`) waits one retry delay and repeats the same
  attempt ("retry"), which settles through its own recorded operation if it got
  that far. The stall guard and the caller's deadline still bound it.
- The lineage read after a refused attempt waits out a busy database for up to the
  writer wait window, so the retry is still named by its parent.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_busy_append_repeats_its_attempt.py`
- `CHANGELOG.md`
