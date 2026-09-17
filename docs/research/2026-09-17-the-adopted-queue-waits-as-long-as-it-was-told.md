# The adopted queue waits as long as it was told

Dated 2026-09-17. Finding M4 of the third audit (queue slice). The research before the fix.

Files: scripts/memory_queue.py, tests/test_the_adopted_queue_honours_retry_after.py

## What was found

- `QueueFailure.retry_after` carries what a provider said about when to come back: a count
  of seconds, a moment, or the text of a `Retry-After` header. The legacy queue reads it
  (`MemoryQueue._reschedule_failed_task` → `_retry_after_seconds` → `_RETRY_AFTER_READERS`)
  and puts the task back no sooner than that.
- The adopted queue never reads it. `_apply_failure_state` → `_retry_available_at` computes
  only the full-jitter backoff, whose ceiling is `retry_cap_seconds`. A failure with
  `retry_after=3600` left the task ready a few seconds out. A provider that answers 429 with
  a long wait is asked again within seconds, all attempts burn within minutes, and the task
  dies.
- Code graph: `_retry_available_at` ← `_apply_failure_state` ← `_QueueV3CandidateReader.fail`
  ← `_fail_lease` ← the worker, and the compat `drain_with`. The tests at
  `tests/test_memory_queue.py` for Retry-After cover the legacy queue only.

## Practice on this date

- "The HTTP `Retry-After` response header indicates how long the user agent should wait
  before making a follow-up request." and, for the case at hand, "In a `429 Too Many
  Requests` response, this indicates how long to wait before making a new request."
  ([MDN, Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After)).
  A client's own backoff is a floor it chooses; the server's figure is a floor it was given.
  The wait is the larger of the two.

## The decision

- The reader of `retry_after` becomes a module function, used by both queues, so there is
  one reading of the value and one bound (`_MAX_RETRY_AFTER_SECONDS`).
- `_retry_available_at` takes the failure's `retry_after` and returns the later of the
  jittered backoff and the stated wait. A task that is not going back to `ready` is
  unaffected.
