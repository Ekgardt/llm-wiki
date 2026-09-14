# One second chance per lineage, and no task stuck between states

Dated 2026-09-14. Items 2.1, 2.2 and 2.8 of `docs/AUDIT-2026-09-14-2.md`, and the
legacy redrive named in the same section. The research before the fix.

## What was found

- **2.1, the redrive bound.** The approved rule
  (`docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`): a dead task
  gets one redrive, "третьего не будет". `_require_redrivable` reads the dead task's
  own `lineage_generation`, which the trigger `queue_lineage_insert` increments on the
  *parent* when a child is inserted. The child starts at 0, so when the child dies it
  can be redriven, and its child too: the audit reproduced four redrives in a row.
  The legacy `MemoryQueue.redrive` (v2) checks only `state == 'dead'`, no bound at all.
- **2.2, the stuck task.** `claim` takes `attempts < max_attempts` and increments
  `attempts` as it leases (`_take_task_lease`). The v2 queue settles an expired lease
  with `_EXPIRED_LEASE_OUTCOMES`: `dead`/`attempts_exhausted` when the attempts are
  spent, `ready`/`lease_expired` otherwise, and its startup retires `ready` rows at the
  limit (`test_startup_retires_legacy_ready_task_at_attempt_limit`). The v3
  `recover_expired_leases` always writes `ready`: a lease that expired on the eighth
  attempt leaves a `ready` task that no claim takes (attempts 8) and no redrive accepts
  (not dead). Reproduced by the audit.
- **2.8.** The v3 `claim` never recovers expired leases; only
  `flush_memory` calls `recover_expired_leases`, so a non-capture task whose worker
  died waits for the next capture worker.
- Read-only on the live `run/queue-v3.sqlite3` today: 25 dead, 44 succeeded,
  23 cancelled `flush` tasks; 23 tasks are redrives, none a redrive of a redrive, none
  dead; no `ready` task at 8 attempts. Nothing to repair in place; the defects are
  latent.
- The code graph: `_require_dead_task` ← v3 `redrive` ← `memory_queue.redrive` ←
  `_cli_redrive`, `mcp_server._doctor_queue_redrive`. `recover_expired_leases` ←
  `flush_memory`. `lineage_generation` is also read by the corruption/quarantine
  evidence, so its meaning must not change.

## Practice on this date

- A dead-letter redrive is an operator decision, and a message "comes back as if it
  had just arrived" (sources in the 2026-09-07 note); a bound on redrives is this
  vault's own choice because nobody watches the queue. A bound that a copy resets is
  no bound: AWS SQS counts `maxReceiveCount` per message and moves it to the DLQ when
  it is reached, and a redriven message keeps its identity
  ([SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)).
- A task whose lease expired on its last attempt is exhausted, not ready: the same
  rule this queue's v2 applies (`_retire_expired_lease`).

## The decision

- The redrive bound counts the lineage: a dead task may be redriven only if neither it
  has been redriven (`lineage_generation`) nor it is itself `MAX_REDRIVE_GENERATIONS`
  redrives deep (its `redrive_of` chain, read with a bounded recursive query). One
  helper, used by the v3 and the legacy v2 `redrive`. `lineage_generation` keeps its
  meaning.
- The v3 recovery settles an expired lease like v2: `dead`/`attempts_exhausted` at the
  limit, `ready`/`lease_expired` below it; and a `ready` task already at the limit is
  retired to `dead` in the same pass.
- The v3 `claim` runs that recovery in its transaction before choosing a task, as v2's
  `claim` does.

Files: `scripts/memory_queue.py`, `tests/test_one_second_chance_and_no_stuck_task.py`,
`docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
