# A transaction and its images agree: no directory without a row, no row waiting on a directory

Date: 2026-09-25. Audit items C-8 and C-9 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on the live vault)

- `_prepare_new_transaction` creates `run/transactions/<id>/` and then inserts
  the `preparing` row, outside the writer gate. `_insert_preparing_row` removes
  the directory when the insert meets an existing operation id, but not when it
  fails otherwise (a deadline, a busy database): the directory stays with no row.
  Live: 9 such directories, 18 to 27 days old.
- `_prune_one` returns without doing anything when a settled row has no
  directory, so `artifacts_pruned_at` is never set, the row is offered again on
  every prune, and the history prune — which requires `artifacts_pruned_at` —
  never takes it. Live: 13 settled rows without a directory.

## Source

- SQLite, "Atomic Commit In SQLite", https://www.sqlite.org/atomiccommit.html
  (fetched 2026-09-25): "Atomic commit means that either all database changes
  within a single transaction occur or none of them occur." A directory beside
  the database is not one of those changes; the program that made it has to
  reconcile it with the rows itself.

## Decision

- A preparing insert that fails for any reason removes the directory it was
  about to describe, then re-raises.
- The prune marks a settled row whose directory is already gone as pruned
  (there is nothing to keep), so the history prune can take it.
- The prune also removes a transaction directory no row names once it is an hour
  old — far past any prepare, which inserts its row within seconds — so the
  9 existing ones go and a future crash between the two steps is repaired.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_transaction_and_its_images_agree.py`
- `CHANGELOG.md`
