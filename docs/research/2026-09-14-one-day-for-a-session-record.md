# One day for a session record

Dated 2026-09-14. Item 2.11 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- A session record's folder is the first ten characters of its `captured_at`
  (`session_evidence._capture_day`).
- The queue capture path stamps `captured_at` with `flush_memory._capture_now()` —
  `datetime.now().astimezone()`, the local date with its offset.
- The detached flush path (`flush_memory._keep_transcript_record`) stamps it with
  `datetime.now(timezone.utc)`: the UTC date.
- Episode consolidation's "today" and "yesterday" are local
  (`episode_consolidation._default_day`, `pending_days`).
- On a machine west of UTC, a session that ends in the local evening is filed under the
  next day by the detached path and under the same day by the queue path; the same
  session captured by both paths lands in two folders, and consolidation can treat the
  UTC-dated folder as "today" and skip it, or read it a day early. This machine runs in
  UTC, so it does not show here (stated from the code).
- `backfill_sessions._session_day` also uses UTC, from the transcript's mtime. Records
  already written by it are addressed by that day (`_record_exists`); changing it would
  write every past session a second time under a local day, so it is left as it is.
- The code graph: `_keep_transcript_record` ← `flush_memory` detached `main` path;
  `_capture_now` ← `process_new_capture`, `_keep_session_record` callers.

## Practice on this date

- One clock convention per partition key: a record's partition must be derivable the
  same way by every writer and by the reader that selects partitions, or one event
  spans two partitions (ISO 8601 dates carry no zone; the zone is the writer's choice
  and must be one choice).

## The decision

- The detached path stamps `captured_at` with `_capture_now()`, the same local, aware
  time the queue path uses, so both paths and consolidation agree on the day.
- Backfill keeps its UTC day, for the reason above.

Files: `scripts/flush_memory.py`, `tests/test_one_day_for_a_session_record.py`,
`docs/research/2026-09-14-one-day-for-a-session-record.md`.
