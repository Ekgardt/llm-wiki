# A lost fence is not a lost capture

Date: 2026-09-11. Trigger: the doctor's capture check reads `degraded — 4
capture(s) were lost` (`logs/doctor-report.json`); the session-start block
repeats it. Three of the four, and every `adapter_capture_worker` "lost" row
since 2026-09-07 in `logs/capture-failures.jsonl`, carry
`QueueOperationError: intent_fence_lost`.

## What those rows were

Read on 2026-09-11 from the live `run/queue-v3.sqlite3` (read-only):

- every capture intent of 2026-09-10 and 2026-09-11 — including the ones
  published at 16:06:52, 19:33:24/39 and 09:19:34, the moments the "lost"
  rows were written — has a `capture_task_links` row, and every linked task
  is `succeeded`;
- `ready` intents without a task: 0.

So the capture each "lost" row names was completed. The pattern in the log is
a worker deferred by `owner_busy`, then a successor that found the intent
fence held by the other owner and raised `intent_fence_lost`. Losing the fence
is losing *authority*, not data: the intent is durable before any worker runs
(`knowledge/notes/durable-capture-producer-activation-decision.md`), a worker
fence that lapses leaves its task's lease to expire and be recovered, and a
capture fence lost before the task exists leaves a `ready` intent that
`capture_adoption` dispatches (`docs/research/2026-08-28-adopting-an-orphaned-intent.md`).

## Sources

1. `capture_diagnostics.is_contention` (#26.3): a writer race is decided by the
   exception's type and code, never by its text; a race is retried work, not
   lost work.
2. Transactional-outbox practice the adoption module already follows: the
   durable row is the record of work; a relay that loses its claim leaves the
   row for the next relay (Richardson, "Pattern: Transactional outbox",
   https://microservices.io/patterns/data/transactional-outbox.html).

## Decision

`QueueOperationError` with code `intent_fence_lost` is contention: the record
says `deferred`, and the doctor stops counting it as a loss. The code is read
from `error.code`, never from the message. Any other `QueueOperationError`
stays a loss. Captures that really never complete remain visible where they
are decided: `capture_adoption` reports `ready` intents it could not adopt,
and a task that exhausts its attempts is `dead`.

Files: `scripts/capture_diagnostics.py`, `tests/test_capture_diagnostics.py`,
`CHANGELOG.md`.
