# A day that failed is tried again

Dated 2026-09-14. Item 2.1 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

`episode_consolidation.py` turns a day of session records into grounded lessons
appended to the daily log. The nightly runs it for yesterday only
(`scheduled_nightly._episode_step`, no `--all-pending`), with a 300-second step
timeout.

- **A failed day is never tried again.** A day is recorded in
  `run/state.json → consolidated_session_days` only when every batch finished. If
  the provider is down, or a reply does not parse, the day is left unrecorded — and
  the next night asks for the next yesterday. Checked on the live state: days
  2026-08-26, 08-27, 08-29, 09-04 and 09-08 have session records under
  `knowledge/raw/sessions/` and are not in `consolidated_session_days`.
- **"Provider down" and "unparseable" read the same.** `_call_provider` turns a
  `None` from `call_llm` into `""`, and `_json_array("")` raises
  `consolidation did not answer with a JSON array`.
- **The reply reader takes the first `[` to the last `]`.** A reply with
  `[[wikilink]]` or `- [x]` before the array, or `[2]` after it, is refused
  (reproduced by the audit with the real function).
- **A partial day duplicates.** Batches are written one by one; if batch 2 fails
  after batch 1 was appended, the day stays pending, and a rerun appends batch 1
  again under a different operation id, because the model's second answer differs.
- **A day in progress could be closed early.** `pending_days` lists every directory
  with records, today's included; a catch-up run would mark today consolidated at
  noon and never read its evening.

## Practice on this date

- Spring Batch's chunk-oriented restart: the reader's position "is saved … on every
  chunk commit, inside the same transaction as that chunk's writes", so a restarted
  job resumes at the end of the last chunk that actually succeeded — progress is
  checkpointed per unit, keyed by the unit, not inferred from output
  ([configuring a step for restart](https://docs.spring.io/spring-batch/reference/step/chunk-oriented-processing/restart.html),
  [restart a job on failure](https://www.baeldung.com/spring-batch-restart-job-failure-continue)).
  A retry that can never succeed is bounded and recorded rather than paid for
  forever.
- Here the batch's append and its checkpoint are two writes (the daily log's
  transaction, then `run/state.json`), not one transaction. A crash between them
  re-runs that one batch; the residual risk is a duplicate entry for that batch
  only, stated here rather than hidden.
- The reply-reading ladder adopted today for answers
  (`docs/research/2026-09-14-the-document-after-the-notes.md`): a fence, the whole
  reply, else the last complete JSON value written after notes — here, the last
  array whose items are objects (or an empty array), so a bracketed footnote is not
  taken for the answer.

## The decision

1. **One reader of JSON replies, in its own module.** `scripts/reply_json.py` holds
   `reply_document` (moved from `query_memory`, which re-exports it) and adds
   `reply_array`. Episode consolidation reads its reply with `reply_array`.
2. **No reply is not a reply.** A provider that returns nothing raises
   `ConsolidationUnavailable`; the run prints that and stops for the night — every
   other day would fail the same way — and the day stays pending.
3. **Progress is checkpointed per batch.** A batch's key is the digest of the vault
   path, the day and every record's bytes in the batch. After a batch finishes —
   with lessons or without — its key is recorded under
   `consolidation_progress[day]` with the running item count; a rerun skips finished
   batches, so nothing is appended twice. A batch whose reply fails to parse
   `MAX_BATCH_ATTEMPTS = 3` times is recorded as failed and skipped, named in the
   day's outcome. The day is marked consolidated when every batch is finished or
   failed.
4. **The nightly catches up, inside its step.** It runs
   `--all-pending --budget-seconds 240`: every pending day before today, oldest
   first, no new batch started after the budget. Today is never pending.

Why not the alternatives:

- **Mark a failed day consolidated.** It is exactly the silent loss the audit found.
- **Retry the whole day.** It repeats appended batches.
- **Run the catch-up by hand.** The owner's standing requirement is that this works
  without them.

Files: `scripts/reply_json.py`, `scripts/query_memory.py`, `scripts/fact_keys.py`,
`scripts/aggregation_pass.py`, `scripts/episode_consolidation.py`,
`scripts/scheduled_nightly.py`, `tests/test_a_day_that_failed_is_tried_again.py`,
`docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
