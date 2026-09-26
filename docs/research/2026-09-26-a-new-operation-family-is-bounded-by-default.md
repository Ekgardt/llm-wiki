# A new operation family is bounded by default

Date: 2026-09-26. Audit 2026-09-26, finding C-11; corrects the claim of
`docs/research/2026-09-24-every-store-has-a-bound.md` that the coordinator history
has a bound.

## What was measured (live vault, read-only, 2026-09-26)

- `run/markdown-transactions-v3.sqlite3`, 58 MB. By page: `project_checkpoints`
  19.1 MB, `transaction` 18.9 MB, `operation` 7.1 MB.
- Committed transaction rows by family: `post-tool` 14 764, `project` 6 569,
  `user-prompt` 1 521, `session-evidence` 305, everything else under 100 each.
- The 2026-09-25 prune removes only `post-tool` and `user-prompt`. `project` and
  `session-evidence` rows, and every family added later, had no bound: the claim
  "every store has a bound" was wrong for them.
- `project_checkpoints`: 6 570 rows. For each project that has a journal, the sum of
  its `event_json` equals the journal's size: `agenticos` 305 066 bytes of events,
  journal 305 690; `no-hands` 4 207 118 vs 4 241 615; `llm-wiki` 4 901 205 vs its
  sealed segments plus `journal.md` (4 935 436). Checkpoints are the journal's own
  store — `rebuild_journal` rebuilds a deleted project from them (issue #20) — so
  their size is the owner's project history, not a leak. They stay.

## Source

SQLite, "SQLite Foreign Key Support", fetched 2026-09-26 from
https://www.sqlite.org/foreignkeys.html:

- "if the foreign key column for an entry in the track table is NULL, then no
  corresponding entry in the artist table is required."
- "NO ACTION: Configuring "NO ACTION" means just that: when a parent key is modified
  or deleted from the database, no special action is taken." Its example: deleting a
  parent a child row still refers to fails with "foreign key constraint failed".

`project_checkpoints.transaction_id` references `"transaction"(id)` with no action,
so a committed checkpoint must let go of its transaction (set it to NULL) before
that row can be deleted.

## Decision

- Invert the rule: keep by name, prune by default. `KEPT_OPERATION_FAMILIES` names
  the families other records read back by operation id — `compile` and
  `compile-quarantine` (receipts, `compile_memory`, `evidence_resolver`),
  `archive-remove` (daily archive, `archive_daily`), `capture-markdown` (capture
  decisions) and `episodes`. Every other settled row, images pruned, older than 90
  days, goes. A family added later is bounded without anyone listing it.
- A committed checkpoint's transaction pointer is cleared when that transaction is
  settled past the window; the checkpoint row keeps its event, and the journal still
  rebuilds from it (tested). Unsettled checkpoints keep theirs, so recovery is
  unchanged.
- Why it is safe to drop a row whose content is in Markdown: the row still answers
  only a replay of the same operation. Replay sources end long before 90 days — hook
  replays arrive within seconds, queue tasks are kept 30 days
  (`queue_result_retention_days`).

## Against recurrence

`tests/test_a_new_operation_family_is_bounded_by_default.py` prunes a family nobody
listed, keeps each name in `KEPT_OPERATION_FAMILIES`, and rebuilds a journal after
its checkpoint's transaction is gone.

## Files

- `scripts/markdown_transaction.py`
- `tests/test_a_new_operation_family_is_bounded_by_default.py`
- `tests/test_every_store_has_a_bound.py`
- `docs/USER-GUIDE.md`
