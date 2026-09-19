# A session is filed under the day it happened

Dated 2026-09-17. The remainder of finding C-F10 of the third audit, left to the owner by the
first round and delegated back: which day a session that was processed later than it ended
belongs to, and what the consolidation must do about it.

## What was found

- `integration_adapter._capture_source_record` hard-codes `"occurred_at": None`, although the
  envelope it is built from carries the moment the hook fired. So the only time the capture
  worker has is its own clock: `_keep_session_record` stamps `captured_at = now()` and
  `process_new_capture` chooses the daily log by `now()`.
- The consequences are the ones the audit named: a queue drained the next morning files
  yesterday's sessions under today, and a retry after midnight writes a second session record
  under a second day, because the record's path is derived from that stamp.
- The obvious repair collides with the nightly consolidation. `episode_consolidation.pending_days`
  skips a day that `consolidated_session_days` names, and `_skip_reason` refuses it outright, so a
  record filed under its true — already consolidated — day would never be read for lessons. That
  is what made this "one decision across capture and consolidation".
- What the consolidation already has: `_record_consolidation` stores, per day, the moment it ran
  and how many records it covered; batch keys are derived from the bytes of the records in the
  batch, and `_logged_batches` finds the keys already written into the logs. So a second pass over
  a day pays a provider only for a batch it has not written before.

## Practice on this date

- Event time and processing time are different clocks, and a record that cannot tell them apart
  cannot be filed correctly: "In any data processing system, there is a certain amount of lag
  between the time a data event occurs (the "event time", determined by the timestamp on the data
  element itself) and the time the actual data element gets processed at any stage in your pipeline
  (the "processing time", determined by the clock on the system processing the element)"
  ([Apache Beam, Basics of the Beam model](https://beam.apache.org/documentation/basics/),
  fetched 2026-09-17). The capture worker had only the second clock.
- A system that groups by event time needs a name and a rule for what arrives after its group
  closed: "After the watermark progresses past the end of a window, any further element that
  arrives with a timestamp in that window is considered _late data_" (same page). Our day is that
  window, the nightly consolidation is its watermark, and until now late data was dropped
  silently.
- A host's clock is not trusted without a bound: a timestamp far from now is a broken clock, not
  a late session, and filing under it would scatter entries into arbitrary days.

## The decision

- The intent carries the session's own time: `occurred_at` is the envelope's moment, as an ISO
  string. It is part of the intent's identity, so intents published from now on are identified by
  it too; existing intents keep the identity they were published with.
- The capture worker files by that time, converted to the local day, for both the session record
  and the daily entry. The worker's clock remains the fallback when the intent carries no time,
  and a time more than `MAX_BACKDATED_CAPTURE_DAYS` (30) away from now — in either direction — is
  a broken clock and is not used. A retry therefore chooses the same day as the first attempt,
  which is the other half of the finding.
- The consolidation reopens a day whose records changed since it ran: `pending_days` and
  `_skip_reason` compare the day's record count with the count stored for it, and a day with more
  records is pending again. The second pass costs one provider call for the batch that holds the
  late record, because every batch already written is found by its marker in the log.

Files: `scripts/integration_adapter.py`, `scripts/flush_memory.py`,
`scripts/episode_consolidation.py`,
`tests/test_a_session_is_filed_under_the_day_it_happened.py`
