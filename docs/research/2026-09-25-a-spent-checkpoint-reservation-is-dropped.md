# A spent checkpoint reservation is dropped; the checkpoint log keeps growing by design

Date: 2026-09-25. Audit items C-11 and C-12 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on the live coordinator)

- A retry of a checkpoint whose first attempt was spent takes the next attempt
  ordinal; the old attempt row stays `reserved`. Live: 6 570 checkpoints, all
  `committed`; 41 attempts `reserved` (2026-08-28 to 2026-09-24), every one of
  them on a checkpoint another attempt committed.
- `prune_history` drops attempts of committed checkpoints only once they are
  past the 90-day window, so these reservations stand until late November and,
  while they name a transaction, keep that transaction out of the history prune.
- C-12: `project_checkpoints` is not pruned, deliberately: "Checkpoint rows
  stay: they are the log `rebuild_journal` rebuilds a project from"
  (`docs/research/2026-09-24-every-store-has-a-bound.md`). 12 MB of event JSON
  on 2026-09-25. That growth is the evidence log the contract keeps, not a leak;
  a compact or archive tier for it is a design question left open here.

## Source

- PostgreSQL, `PREPARE TRANSACTION`,
  https://www.postgresql.org/docs/current/sql-prepare-transaction.html (fetched
  2026-09-25): "It is unwise to leave transactions in the prepared state for a
  long time ... a prepared transaction will normally be committed or rolled back
  as soon as" its outcome is known. A reservation whose checkpoint is decided is
  in that position.

## Decision

- `prune_history` drops every `reserved` attempt whose checkpoint is committed,
  at once, before the 90-day prune of the rest. `quarantined` attempts keep
  their 90 days as evidence; committed attempts are unchanged.
- Nothing about `project_checkpoints` changes.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_spent_checkpoint_reservation_is_dropped.py`
- `CHANGELOG.md`
