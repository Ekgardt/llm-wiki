# A day is consolidated whole, and reopened when it grows

Dated 2026-09-17. The leftovers of finding M-A13 of the third audit: the 240-record day
ceiling, and a record written for a day that was already consolidated. The research before
the fix, and the rule the capture area asked for (their C-F10).

Files: `scripts/episode_consolidation.py`,
`tests/test_a_day_is_consolidated_whole_and_reopened_when_it_grows.py`.

## What was found

- `session_records` returns the first 240 record files of a day, sorted by name, and
  `consolidate_day` then records the day as consolidated with the count it actually read.
  A day with more records is silently cut, and it is cut in exactly the way the docstring
  of `record_batches` was written against ("A day used to be truncated to the first twelve
  records, which was invisible and wrong the moment a day held more: the imported history
  has a day with 171 sessions"). The ceiling is real work bounding — twenty provider calls
  is already a very busy night — but it belongs to the run, not to the day.
- A record written for a day after that day was consolidated is never read: `_pending` asks
  only whether the day has an entry in `consolidated_session_days`. This is not exotic —
  a session that starts before midnight and is captured after it, a hook that writes its
  evidence late, an imported history unpacked after a nightly pass.
- Re-reading a day is cheap, because the day's own daily log already names every batch that
  was written (`BATCH_MARKER`), and `_batches_to_find` uses those markers to finish a batch
  without calling a provider. What costs is a batch whose *membership* changed: records are
  chunked in sorted order, session names are random, so a late record can shift the chunk
  boundaries and make the batches after it new work.

## Practice on this date

- Incremental, restartable batch work is checkpointed by what was done rather than by a
  flag on the whole unit: "Checkpointing … allows the job to restart from the last
  successful checkpoint rather than from the beginning" — the checkpoint here is the batch
  marker in the daily log plus the per-day progress record, and both already exist
  (Kleppmann, *Designing Data-Intensive Applications*, ch. 10, on batch job restartability).
- The vault's own contract says Markdown is the authority and runtime state is derived
  (`CLAUDE.md`, Stage 2 operational contract). So "has this day been consolidated" must be
  answerable from the day's own records and its daily log, and the state entry is a cache
  of that answer — which is exactly what a stored record-set digest makes it.

## The decision

- The 240-record ceiling becomes a per-run bound, not a per-day one: every record of the
  day is batched, at most twenty batches are attempted in one run, and the day is recorded
  as consolidated only when every batch of it is done. A day larger than one run's bound
  stays pending and the next run continues it, skipping the batches the log already names.
- A consolidated day stores the digest of its sorted record names beside its count. A day
  whose records have changed since is pending again. The cost of a reopened day is one
  provider call per batch whose membership changed; identical batches are recognised by
  their markers and cost nothing.
- **The rule for the capture area (C-F10):** capture never has to decide which day a late
  session belongs to for consolidation's sake. Write the session record under the date it
  belongs to — the date its session started — even when that day is closed. Consolidation
  notices that the day's record set changed and reads the day again; nothing is lost and no
  extra call is made for records that were already read. What capture must *not* do is move
  a late record into today's directory to make it visible: that would date the evidence
  wrongly, and the day's own reopening already covers it.
- No path, environment variable or contract changes.
