# The small queue gaps of the third audit

Dated 2026-09-17. Findings L12, L16, L17 and the proven-dead functions of the third audit
(queue slice). The research before the fixes.

Files: scripts/memory_queue.py, scripts/markdown_transaction.py,
tests/test_the_worker_does_not_call_a_broken_database_idle.py,
tests/test_the_worker_writes_under_the_vault_not_the_current_directory.py

## What was found

- L17. `_claim_when_reachable` exists so that a busy database means "try again shortly".
  It returns `None` — "nothing to claim" — for every other `sqlite3.OperationalError` too:
  a disk I/O error or a missing table makes the worker report an idle queue and exit 0.
- L16. `_daily_log_path` and `_manual_compile` default `LLM_WIKI_ROOT` to `"."`. The rest
  of the module resolves the vault as the scripts' parent (`_vault_root`). `memory_queue.py
  work` started from another directory without the variable appends flushed memory under
  that directory and marks the task succeeded.
- L12. The docstring of `retry_unsettled_sequence` says it is called by one caller "and by
  nothing else"; the graph shows three: `project_journal`, `repair_journal_gap` and
  `repair_orphaned_checkpoint_names`.
- Dead code, proved by grep over `scripts/`, `integrations/`, `benchmark/`, `.github/`,
  `docs/`, `tests/` and the install scripts: `_queue_dir`, `recover_stale_leases` (ignores
  its argument) and `retained_queue_state` have no reference but their own definition.
  No contract in `CLAUDE.md` or `docs/STRUCTURE.md` names them.

## Practice on this date

- SQLite separates the two conditions by result code. Busy: "The SQLITE_BUSY result code
  indicates that the database file could not be written (or in some cases read) because of
  concurrent activity by some other database connection, usually a database connection in a
  separate process." I/O error: "The SQLITE_IOERR result code says that the operation could
  not finish because the operating system reported an I/O error."
  ([SQLite result codes](https://www.sqlite.org/rescode.html)). Only the first is a reason
  to wait; the second is a failure the operator has to see.

## The decision

- L17: a busy database past the deadline still answers `None`; any other
  `OperationalError` is raised.
- L16: both functions resolve the vault through `_vault_root()`, the one rule the module
  already has.
- L12: the docstring names its callers.
- The three unreferenced functions are deleted.
