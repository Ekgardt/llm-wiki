# A worker that failed lost no capture, and says why it failed

Dated 2026-09-14. Section 4 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

- Doctor and the session-start block report "1 capture(s) were lost". The row, in
  `logs/capture-failures.jsonl`: `2026-09-14T09:17:21`, kind
  `adapter_capture_worker`, outcome `lost`, reason
  `ReliabilityV3ValidationError: reliability_v3_record_invalid`.
- Nothing was lost. Read on the live `run/queue-v3.sqlite3` (read-only): all 68
  capture intents have a `capture_task_links` row, and the next capture at 10:59 was
  processed. The failing process was a capture *worker*: it runs only over intents
  that were published durably before any worker starts, and a worker that fails
  leaves them for the next worker or for `capture_adoption`
  (`docs/research/2026-09-11-a-lost-fence-is-not-a-lost-capture.md`). A worker run can
  fail; it cannot lose a capture.
- The cause cannot be known. `installed_memory_repair.require_reliability_v3_adopted`
  wraps any exception into `ReliabilityV3ValidationError("reliability_v3_record_invalid")`
  `from exc`, and the failure trail records `describe_error(error)` — the outer class
  and message only. The chained cause, which names the file and the problem, is
  dropped. The same check passed at 0.21 s when run by hand afterwards.

The 2026-09-11 fix classified one worker failure code (`intent_fence_lost`) as
contention. The class is wider: every failure of a worker run is retried work.

## Practice on this date

- Transactional outbox: "the durable row is the record of work; a relay that loses its
  claim leaves the row for the next relay" — the relay's failure is an availability
  event, not a data-loss event
  ([Pattern: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html)).
- Python keeps the cause of a re-raised exception in `__cause__` precisely so a
  wrapper does not hide it ([PEP 3134, exception chaining](https://peps.python.org/pep-3134/)).

## The decision

1. **A capture worker's failure is `deferred`, never `lost`.**
   `capture_diagnostics` treats kind `adapter_capture_worker` as retried work
   whatever the exception, as it already does for contention. A capture event (session
   end, pre-compact) that fails before its intent is durable is still `lost`.
2. **The trail names the cause.** `secret_redact.describe_error_chain` renders
   `Class: message` for the error and up to two causes (`__cause__`, else
   `__context__`), each redacted, joined by ` <- `; the adapter records failures with
   it.

The historical 09:17 row stays as written; it leaves the doctor's window after seven
days (`CAPTURE_RECENT_SECONDS`).

Files: `scripts/capture_diagnostics.py`, `scripts/secret_redact.py`,
`scripts/integration_adapter.py`, `tests/test_a_worker_that_failed_lost_no_capture.py`,
`docs/research/2026-09-14-a-worker-that-failed-lost-no-capture.md`.
