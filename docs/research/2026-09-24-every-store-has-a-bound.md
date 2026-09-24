# Every store has a bound

Dated 2026-09-24. Audit items B-6 (throwaway checkouts), B-7, B-13 (retention), C-3, C-4
and C-5 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/ephemeral_paths.py` (new), `scripts/retire_own_call_transcripts.py`,
`scripts/repository_index.py`, `scripts/repository_retention.py`,
`scripts/repository_worktrees.py`, `scripts/markdown_transaction.py`,
`scripts/reclaim_runtime_state.py`, `scripts/memory_state.py`,
`scripts/maintenance_helpers.py`, `scripts/doctor.py`, `scripts/scheduled_weekly.py`,
`scripts/scheduled_nightly.py`, `scripts/retire_benchmark_runs.py` (new),
`tests/test_every_store_has_a_bound.py` (new), `CHANGELOG.md`, `docs/STRUCTURE.md`,
`docs/research/2026-09-24-every-store-has-a-bound.md`.

## What was found (live vault, 2026-09-24)

- **Transactions (B-7).** `run/markdown-transactions-v3.sqlite3` is 55 MB: 23 557
  transaction rows (oldest 2026-08-20), 30 744 operation rows, 6 633 checkpoint attempts.
  The nightly prunes the *images* of settled transactions after the two-day undo window
  (`MarkdownCoordinator.prune`), never the rows. About 670 rows a day. Doctor reads
  10 000 of them and calls its counts lower bounds.
- **The queue (B-13).** `run/capture-intents/ready/` holds 73 intents, 55 MB; 199 empty
  shard directories under `pending/`. The designed retention exists —
  `memory_queue purge --terminal-before … --export …` exports finished tasks with their
  intents and decisions, then deletes them, and `DEFAULTS.queue_result_retention_days`
  is 30 — but nothing runs it: the weekly only prints `memory_queue status`.
- **Hook log (C-3).** `logs/hook-errors.log` is 924 KB; `trim_scheduler_logs` bounds four
  scheduler logs and not this one.
- **Benchmarks (C-4).** `cache/benchmarks/` is 1.5 GB: five dataset caches named by the
  `DATASET_DIR` constants of `benchmark/*_data.py`, and four run directories written by
  operator commands (`full-2026-09-14/17/18`, `locomo-2026-09-19`), with nothing removing
  either.
- **Run debris (C-5).** 26 `.llm-wiki-lock-probe-*.sqlite3` files (2026-08-28..09-07,
  from before the probe cleaned up after itself) sit in `run/`. `memory_state._keep_previous`
  hard-links the state file to a staged name and renames it onto `.previous`; when
  `.previous` already is that inode, POSIX `rename` "does nothing, and returns a success
  status" (rename(2)), so the staged link stays: six today, swept only by the nightly.
- **Throwaway checkouts (B-6).** The nightly spends about 8 minutes and keeps about 1.9 GB of
  code generations for three checkouts of one repository, one of them a temporary clean
  worktree under the host's job directory.

## Practice on this date

- Every store that only grows needs an owner that bounds it; retention is part of a
  store's design, and a designed retention that is never scheduled is a leak
  (the same finding as `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`).
- rename(2), Linux man-pages: "If oldpath and newpath are existing hard links referring to
  the same file, then rename() does nothing, and returns a success status."

## The decisions

1. **Transaction history, 90 days.** `MarkdownCoordinator.prune_history` deletes, older than
   90 days (the hot window the archive contract already uses): transaction rows that are
   committed or discarded, whose images are already pruned, and that no checkpoint row
   names (their operations cascade); and attempt rows of checkpoints that committed.
   Checkpoint rows stay: they are the log `rebuild_journal` rebuilds a project from.
   Quarantined transactions stay: they are evidence the `run/` contract names. The nightly
   reclaim step runs it after the image prune. Doctor adds exact state totals from one
   `GROUP BY`, so the scan bound no longer hides a count.
2. **Queue, 30 days.** The weekly runs the designed purge: tasks terminal for more than
   `queue_result_retention_days`, dead ones included, exported with their intents and
   decisions to `knowledge/raw/queue-archive/<date>/` (private: `knowledge/raw/**` is denied
   by `.gitignore`, and the nightly snapshot copies it), then deleted from `run/`. Empty
   intent shard directories are removed by the reclaim step.
3. **Hook log.** `logs/hook-errors.log` joins the logs `trim_scheduler_logs` bounds (2 MB).
4. **Benchmarks.** `retire_benchmark_runs.py` (nightly) removes a `cache/benchmarks/`
   directory that is not a named dataset cache and has not been modified for 30 days. Dataset
   caches stay.
5. **Run debris.** The reclaim step removes lock probes older than an hour, as it removes
   staged temporaries; `_keep_previous` unlinks its staged name whatever `rename` did.
6. **Throwaway checkouts.** `ephemeral_paths` names the platform temporary directory and
   the host's job directory (the roots the residue retirer already treats as the memory's
   own). For checkouts only the job directory counts (`is_throwaway_checkout`): a checkout
   in the temporary directory goes when the system clears it and is then retired as
   missing, and test fixtures live there. The nightly neither refreshes nor follows a
   checkout in the job directory, and retention retires its generations. An explicit
   index request still works for the day.

## Limits (rule 3)

The exported queue archive and the checkpoint log still grow, as evidence the contracts keep;
they are outside `run/` and inside the nightly snapshot. The 90- and 30-day windows are the
contracts' existing numbers, not new tuning.

## Sources

- rename(2), Linux man-pages — https://man7.org/linux/man-pages/man2/rename.2.html — fetched 2026-09-24.
- `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`; the live vault's `run/`,
  `cache/` and `logs/`, measured 2026-09-24.
