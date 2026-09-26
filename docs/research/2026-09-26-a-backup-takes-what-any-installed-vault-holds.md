# A backup takes what any installed vault holds

Date: 2026-09-26. Audit 2026-09-26 A-11.

## Facts

- Read-only count on the live vault (`run/markdown-transactions-v3.sqlite3`,
  2026-09-26): 23 664 transaction rows — 23 417 committed with artifacts pruned,
  117 committed with artifacts kept, 13 discarded, 117 quarantined.
- `installed_memory_repair._transaction_blockers` read every row through
  `_bounded_rows`, whose bound is 10 000; past it, `ValueError` became
  `transaction_state_unreadable`. A committed row whose artifacts were pruned
  yields no blocker and no retention, so reading it served nothing.
- `private_vault_backup._ALLOWED_RUNTIME_FINDINGS` refused findings present on
  every installed vault: `install_manifest_retained` (reported for every committed
  install by `install_control`), `retired_operational_database_retained` (the
  retired pre-v3 databases kept after adoption), and a quarantined or conflicted
  transaction, which is evidence kept for the operator. So the backup of the
  installed vault could not complete.
- The guide's recovery order was clone, install, restore, publish. The installer
  creates runtime files and publish refuses any destination with different bytes
  ("Nothing is merged or overwritten"), so that order refuses; `install.sh` treats
  an already adopted vault as `adopted` and keeps it.
- SQLite limits host parameters per statement (https://www.sqlite.org/limits.html,
  "Maximum Number Of Host Parameters In A Single SQL Statement", fetched
  2026-09-26: default 32766 since 3.32.0, 999 before), so an `IN (...)` lookup is
  asked in slices of 500.

## Decision

- The ledger scan reads only rows that can hold something:
  `state <> 'committed' OR artifacts_pruned_at IS NULL`. Artifact directories whose
  id is outside that set are looked up in the ledger by id, so a directory of a
  recorded transaction is never called unknown.
- The backup allowlist adds the four findings above; live owners, nonterminal
  transactions, unknown artifacts and unreadable state still refuse.
- The guide says: clone, restore, publish, then install.

## Files

- `scripts/installed_memory_repair.py`
- `scripts/private_vault_backup.py`
- `docs/USER-GUIDE.md`
- `tests/test_a_backup_takes_what_any_installed_vault_holds.py`
- `CHANGELOG.md`
