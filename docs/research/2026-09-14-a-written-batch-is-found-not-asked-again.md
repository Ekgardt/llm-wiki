# A written batch is found in the log, not asked again

Dated 2026-09-14. Item 2.3 of `docs/AUDIT-2026-09-14-2.md`. My fix of this morning
(`docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`) checkpoints finished
batches, and did not close this gap. The research before the fix.

## What was found

- `_BatchRun.one` writes a batch's lessons to the daily log (`append_daily`), then
  saves the checkpoint in `run/state.json` (`_save_progress` → `update_state`). Two
  stores, two steps.
- If the process is killed between them, or `update_state` raises
  `StateLockTimeout` — a subclass of `OSError`, which `_consolidate_reported` reports
  as "episode consolidation skipped" — the lessons are in the log and the checkpoint
  does not know it. The next run asks the model again. The reply differs, so the
  lessons differ, so `_operation_id` (a hash of the lessons) differs, and
  `locked_append_once` finds no marker: a second block. Reproduced by the audit. The
  retry is also a second model call (rule 4).
- The daily log entry is written to *today's* file (`append_daily` uses
  `datetime.now()`), so a retry on a later night looks at a different file than the
  one holding the first block.
- The entry format is read by `evidence_resolver.daily_entries`: an entry starts at
  the `<!-- llm-wiki-operation: -->` marker and takes its id from the head of its
  **first** content line, so nothing may be put before the header line.
- Checked on this machine: `redact_secrets`, which the appender applies to the block,
  leaves a `<!-- llm-wiki-episode-batch:<64 hex> -->` line unchanged.
- The code graph: `_consolidate_batch` ← `_BatchRun.one` ← `consolidate_day` ←
  `_consolidate_reported` ← `main` (the nightly step `episodes`).

## Practice on this date

- When a side effect and its checkpoint cannot share one transaction, make the effect
  idempotent under a key derived from the *input*, and check for the effect before
  redoing it — the idempotency-key pattern
  ([Stripe, idempotent requests](https://docs.stripe.com/api/idempotent_requests);
  [AWS Builders' Library, making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)).
  A key derived from the output of a non-deterministic step (a model reply) cannot
  recognise a retry.
- The vault's own rule: Markdown is the authority, runtime state is coordination
  (`CLAUDE.md` section 1). The log, not `state.json`, is what proves a batch was
  written.

## The decision

- Each batch's block ends with `<!-- llm-wiki-episode-batch:<batch key> -->` — the key
  `_batch_key` already derives from the vault, the day and the bytes of every record in
  the batch. It is the last line, so the entry's first line and id are unchanged.
- Before asking the model for a batch the checkpoint does not list, the run looks for
  that marker in the daily logs dated from the consolidated day to today (the only
  files a write for that day can be in). Found: the batch is marked done with no call.
  The markers are read once per day consolidated.
- A checkpoint that could not be saved no longer causes a second block or a second
  call; it costs one scan of those logs on the next run.

Why not the alternatives:

- **Derive the operation id from the batch key.** The log marker would be predictable,
  but a transaction left in flight under that id with other bytes would refuse every
  later append for the batch ("operation_id is already bound to a different request").
- **Checkpoint before writing.** A crash after the checkpoint loses the day's lessons
  silently, which is worse than a duplicate.

Files: `scripts/episode_consolidation.py`,
`tests/test_a_written_batch_is_found_not_asked_again.py`,
`docs/research/2026-09-14-a-written-batch-is-found-not-asked-again.md`.
