# A retried checkpoint is the same batch

Dated 2026-09-14. A guess at the end of `docs/AUDIT-2026-09-14-2.md`, confirmed by a
read-only audit today. The research before the fix.

## What was found

- `integration_adapter._drain_project_checkpoint_once` claims pending lifecycle events
  from `run/state.json`, plans a batch (`_observe_until_checkpoint`, `_resolve_debounce`,
  `_batch_plan`), writes the project journal (`_persist_or_release` →
  `_write_project_checkpoint`), and then commits: it deletes the batch from the pending
  queue and saves the reducers (`_commit_or_release`, state lock 0.5 s).
- The journal entry is idempotent by its batch: `_batch_occurrence_id` hashes the event
  ids, and the idempotency key adds the reason; the Markdown reservation collapses a retry
  of the same key.
- The journal write comes before the state commit. If the commit times out, the claims
  are released and the events stay queued. New events arrive meanwhile; the next drain's
  debounce flushes "the newest item" (`len(items) - 1`), so the batch now has more members
  and a new id. The journal gets a second entry repeating the deltas already written.
  A retry with the same members would have been collapsed.
- The code graph: `_drain_project_checkpoint_once` ← `_drain_project_checkpoints` ← the
  adapter's lifecycle handlers and maintenance.

## Practice on this date

- Make the retry of a non-transactional side effect replay the recorded intent, not
  re-plan it: write the intent durably before the effect, and resume from it (a
  write-ahead intent, the pattern this codebase's capture intents already follow).

## The decision

- Before the journal write, the drain records the batch it is about to write —
  its event ids, the reason and the checkpoint time — as
  `project_checkpoint_inflight[<queue key>]` in the state, under the same lock and claim
  check as the commit. If that record cannot be written, nothing is written to the
  journal and the claims are released.
- The next drain of that queue, when an in-flight record's event ids are still the head
  of the queue, replans exactly that batch with that reason — the same batch id, which the
  reservation collapses — instead of a new debounce plan.
- The commit removes the in-flight record together with the batch. A record whose ids are
  no longer the head of the queue is ignored and cleared by the next commit.

Files: `scripts/integration_adapter.py`, `tests/test_a_retried_checkpoint_is_the_same_batch.py`,
`docs/research/2026-09-14-a-retried-checkpoint-is-the-same-batch.md`.
