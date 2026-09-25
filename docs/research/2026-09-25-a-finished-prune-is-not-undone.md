# A finished prune is not undone by its own recovery

Date: 2026-09-25. Audit item C-10 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `_prune_one` renames a transaction's images to `.<id>.pruning-<uuid>`, marks
  the row `artifacts_pruned_at`, then removes the staged directory. A crash
  between the mark and the removal leaves the staged directory behind.
- `_restore_staged_prune` puts every staged directory back under its
  transaction id unless a live one is there — also when the row already says its
  images are pruned. The images come back for a row that disowns them, and no
  later prune takes them, because `_prunable_rows` asks only for rows not yet
  marked.

## Source

- SQLite, "Atomic Commit In SQLite", https://www.sqlite.org/atomiccommit.html
  (fetched 2026-09-25): "either all database changes within a single transaction
  occur or none of them occur." The row's mark is the committed decision; the
  file system has to follow it, not the other way round.

## Decision

- Recovery reads the row: a staged directory whose row is marked pruned, or
  whose row is gone, is removed; only one whose row is still unmarked goes back.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_finished_prune_is_not_undone.py`
- `CHANGELOG.md`
