# The nightly finishes what a publisher left half way

Date: 2026-09-26. Audit 2026-09-26, finding C-12 (capture adoption does not complete
`pending/`; the command exits 0 whatever it skipped).

## What was wrong (facts, read in the code)

- `capture_adoption.adopt_in_active_vault`, which the nightly step `capture_adoption`
  runs, called only `adopt_orphaned_capture_intents`. The pass that finishes a
  half-published intent (`complete_pending_capture_intents`, a `pending` row whose
  publisher died before marking it ready) ran only inside the capture worker, which
  runs only when a capture wakes it — the very thing that may be failing.
- The command printed its skips and returned 0. The capture worker wrote standing
  skips to the capture-failure trail (which doctor reads); the nightly command did not,
  so a skip made there was named nowhere doctor looks.

## Source

Chris Richardson, "Pattern: Transactional outbox", fetched 2026-09-26 from
https://microservices.io/patterns/data/transactional-outbox.html: "A separate process
then sends the messages to the message broker." and "The Message relay might publish a
message more than once. It might, for example, crash after publishing a message but
before recording the fact that it has done so."

Conclusion (mine): the relay has to run independently of the producer that failed, and
every relay step is safe to repeat — both hold for these two passes (create-only files,
replay-safe enqueue), so the nightly can run both.

## Decision

- One list, `capture_adoption.RECOVERY_SWEEPS` (finish pending, then adopt), run by the
  capture worker and by the nightly command alike.
- One recorder, `capture_adoption.record_standing_skips`: a skip that is not a writer
  race goes to the capture-failure trail from either caller. The command still exits 0
  for a skip: the failure is named in the trail doctor reads, and one bad intent does
  not mark the whole night failed.

## Against recurrence

`tests/test_the_nightly_finishes_what_a_publisher_left_half_way.py` requires every
`*_capture_intents` pass in the module to be in `RECOVERY_SWEEPS`, so a pass added and
not wired runs nowhere only until the test fails.

## Files

- `scripts/capture_adoption.py`
- `scripts/flush_memory.py`
- `tests/test_the_nightly_finishes_what_a_publisher_left_half_way.py`
