# A rolled-back quarantine is settled, and empty ready shards are removed

Date: 2026-09-25. Audit items B-16 and C-13 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on the live coordinator)

- A transaction that fails its preconditions at apply time, or whose model
  output the DLP boundary blocks, is rolled back first
  (`_rollback_for_quarantine` → `_rollback_applied_operations`, which marks each
  undone operation `applied = 0`) and then set `quarantined`. An operation that
  cannot be undone keeps `applied = 1`.
- `quarantined` is terminal and nothing ever moves it: the artifact prune and
  the history prune take only `committed` and `discarded` rows, and the `run/`
  deletion contract refuses while any quarantined transaction exists.
- Live coordinator, 2026-09-25: 117 quarantined (114 `precondition_failed`, 3
  `dlp_content_blocked`), 69 MB under `run/transactions/`. All 117 have no
  operation with `applied = 1`: none left anything in the vault.
- C-13: `reclaim_runtime_state.remove_empty_intent_shards` walks
  `run/capture-intents/pending/` only; 136 empty shard directories sit under
  `ready/`.

## Source

- PostgreSQL documentation, `ROLLBACK PREPARED`,
  https://www.postgresql.org/docs/current/sql-rollback-prepared.html (fetched
  2026-09-25): "`ROLLBACK PREPARED` rolls back a transaction that is in prepared
  state." A database treats a rolled-back transaction as finished, not as
  pending review; the conclusion drawn here is that a quarantine whose
  operations were all undone is in the same position.

## Decision

- The nightly reclaim step settles every quarantined transaction with no applied
  operation to `discarded`, keeping its `error_code`; from then on the existing
  prunes treat it like any other discarded row. A quarantined transaction with
  an operation still applied stays quarantined: that one did leave something
  behind.
- The same step removes empty shard directories under `ready/` as it already
  does under `pending/`.

## Files

- `scripts/markdown_transaction.py`
- `scripts/reclaim_runtime_state.py`
- `tests/test_a_rolled_back_quarantine_is_settled.py`
- `CHANGELOG.md`
