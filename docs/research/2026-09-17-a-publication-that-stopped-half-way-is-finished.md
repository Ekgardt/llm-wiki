# A publication that stopped half way is finished

Dated 2026-09-17. The last part of finding C-F12 of the third audit, which the first round
left: the sweeper adopts intents in `ready`, and nothing looks at `pending`.

## What was found

- A capture intent is published in one fenced sequence
  (`integration_adapter._publish_capture_files_and_task`): the record is written to
  `run/capture-intents/pending/…`, indexed as `pending`, written to `ready/…`, marked `ready`,
  the pending copy is removed, and only then is the task enqueued.
- `capture_adoption` reads `ready_capture_intents_without_task`, so it recovers a publisher
  that died between "marked ready" and "enqueued". A publisher killed earlier — the host's
  15 s hook timeout lands here — leaves a `pending` row and a file holding the whole redacted
  session, and nothing ever looks at it again. The session is lost and the copy stays on disk.
- Checked on the product: re-publishing the same bytes is safe. `reliable_memory.durable_publish_file`
  returns `duplicate` when the destination already holds the expected digest, and
  `memory_queue.index_capture_intent_pending` / `mark_capture_intent_ready` return the row's
  existing state when it already matches path, digest and size. So the recovery of a half-published
  intent is the publication sequence itself, run again.

## Practice on this date

- This is the transactional-outbox recovery the module already implements for the later step; the
  literature's rule is the same for both: the record is committed first, and a relay completes the
  dispatch. Adoption already relies on the three properties that make it safe — create-only bytes,
  a self-verifying digest, a self-addressing row — and a `pending` row has all three.
- Recovery must never race the publisher that is still alive. Two things keep them apart: the
  publisher holds a live capture owner and intent fence for the whole sequence, which an adopting
  pass cannot take (it is reported as a skip), and a row is only considered once it has been
  `pending` for longer than the fence's own lifetime.

## The decision

- `memory_queue` gains one read-only method, `pending_capture_intents(limit, older_than=None)`:
  the oldest rows still in `publication_state='pending'`, optionally only those whose `updated_at`
  is older than a stamp. Nothing else in the queue changes.
- `capture_adoption` gains a second pass, `complete_pending_capture_intents`, which verifies each
  row's bytes and re-runs the publication sequence for it. It shares the bound and the skip
  reporting of the adoption pass, and it only considers rows older than
  `PENDING_INTENT_RECOVERY_SECONDS` (60 s — twice the 30 s intent fence).
- The capture worker runs it where it already runs adoption, so a half-published intent is
  finished by the next session end and the pending copy stops holding a session's text.

Files: `scripts/memory_queue.py`, `scripts/capture_adoption.py`, `scripts/flush_memory.py`,
`tests/test_a_publication_that_stopped_half_way_is_finished.py`
