# A late check is unfinished, not broken

Date: 2026-09-25. Audit item A-9 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and reproduced)

- With the default 5 s budget the audit saw `queue: error` and `claims: error` with advice to
  run `--repair`; with 120 s both were `ok`.
- The checks catch `TimeoutError` together with `OSError` and `sqlite3.Error` and then set
  `read_error` and status `error` (`_read_claim_index`, `_queue_v2_check`,
  `_transaction_check`, …). A SQLite read cut by doctor's progress handler raises
  `sqlite3.OperationalError`, which is the same `sqlite3.Error` a corrupt file raises.
- Reproduced on a temporary state root: a present `cache/claims.sqlite3` read with an
  expired deadline gives `error` and "Repair: `uv run python scripts/doctor.py --repair`
  rebuilds the claim index".
- The run/ deletion snapshot is computed separately (`_snapshot_deletion_codes`) and does
  not read these results.

## Source

- SQLite, `sqlite3_progress_handler`, https://www.sqlite.org/c3ref/progress_handler.html
  (fetched 2026-09-25): "If the progress callback returns non-zero, the operation is
  interrupted." An interrupted read is the budget, not the file.

## Decision

- One rule for every check doctor collects (`_unfinished_when_late`): a check that reports
  `error` with `read_error` once the budget has run out becomes the same "Check not completed
  because the doctor time budget was exhausted" result the deferred checks already give,
  `degraded` with `budget_exhausted`. It keeps the check's deletion codes, so nothing it would
  have blocked becomes permitted. A read error inside the budget stays `error`.

## Uncertainty

- A genuine read error that happens after the budget ran out is reported as unfinished; a run
  with a larger budget reports it. That is the cost of not guessing which of the two it was.

## Files

- `scripts/doctor.py`
- `tests/test_a_late_check_is_unfinished_not_broken.py`
- `CHANGELOG.md`
