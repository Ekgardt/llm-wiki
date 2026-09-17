# One failed repair does not cancel the others

Dated 2026-09-17. Finding M-A10 of the third audit (low, confirmed by reading; the lock
leak was suspected and is confirmed here by a test). The research before the fix.

Files: `scripts/doctor.py`,
`tests/test_one_failed_repair_does_not_cancel_the_others.py`.

## What was found

- `_run_repairs` wraps the whole chain in one `try`. The repairs are independent — the
  runtime files, the generation catalog, the transaction log, the queue, the search index,
  the archives, the claim index — but an exception from any one of them skips every repair
  after it, and the report files the failure under `runtime` whichever repair raised it. A
  `ValueError` from the generation-catalog repair therefore reads as "Runtime repair
  failed" and quietly leaves the queue, the index, the archives and the claims unrepaired.
- `_MaintenanceHeartbeat.cleanup` requires the fence *before* running the releasing
  operation. It is used for exactly one thing: releasing `.doctor-index.lock` after an
  index rebuild. So a fence lost during the rebuild leaves that lock file on disk with our
  token in it, and the next pass must wait for the lock's own staleness rule. The release
  itself is token-checked (`_release_lock` unlinks only a file that still holds this
  token), so running it without the fence can never release another owner's lock.
- `_repair_archives_action` appends `{"action": "recover_archives"}` whatever
  `DailyArchiver.recover` returned, and it returns the list of receipts it recovered — an
  empty list when there was nothing to recover. The report then claims work that did not
  happen.

## Practice on this date

- "Isolate failures … a fault in one part of the system should not cascade." The bulkhead
  is the standard name for the pattern and the reason: a shared failure boundary turns one
  fault into an outage of everything behind it (Nygard, *Release It!*, ch. "Stability
  Patterns" — Bulkheads; and Microsoft's Cloud Design Patterns, "Bulkhead", which states
  the intent as "isolate elements … so that if one fails, the others will continue to
  function").
- The fence is the exception to the bulkhead, and this product already says so in its own
  words: work done without the fence is work done outside the maintenance contract, so a
  lost fence must end the pass rather than let the next repair run. The nightly already
  reads that distinction (`_fence_lost_outcome`).

## The decision

- Each selected repair runs inside its own boundary and records its failure under its own
  name (`repair_errors["queue"]`, `["indexes"]`, …), and the chain goes on to the next one.
  Two exceptions end the pass immediately, as they must: `MaintenanceFenceLost` (the fence
  is someone else's now) and `TimeoutError` (the deadline or a lost heartbeat, raised by
  the guard). Both are still recorded under `runtime`, which is where a reader looks for
  "the pass stopped", and every repair that never ran is named as deferred so the report
  does not read as if it had been done.
- `cleanup` runs the releasing operation first and checks the fence afterwards, so a lost
  fence is still reported but the lock is not left behind. Nothing else uses `cleanup`.
- The archive repair records `recover_archives` only when at least one archive was
  actually recovered, and says how many.
- No path, environment variable or contract changes.
