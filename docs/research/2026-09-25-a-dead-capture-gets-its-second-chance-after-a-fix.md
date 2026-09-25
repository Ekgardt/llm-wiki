# A dead capture gets its second chance after a fix

Date: 2026-09-25. Audit items A-5 and B-14 of `docs/AUDIT-2026-09-25-full.md`.

## Question

The weekly queue purge (added 2026-09-24, `--include-dead`) fails as a whole on
the first capture task that has no terminal record, and 25 dead capture tasks have
none. Those 25 sessions were never compiled and nothing moves them. What should
happen to a dead capture, and what should the purge do with one it cannot prove?

## Sources

- Amazon SQS Developer Guide, "Using dead-letter queues" (fetched 2026-09-25,
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html):
  a dead-letter queue isolates what failed so the cause can be found; once it is,
  messages are moved back with a redrive; retention of a dead-letter queue should
  be longer than the source queue's.
- `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`: one
  redrive per lineage; a redrive carries the capture link; in the field a redrive
  is an operator's decision, and here nobody is that operator.

## Findings (facts, live queue read-only)

1. 25 capture tasks are `dead / processor_failed` (created 2026-08-27..09-08), 8
   attempts each, each with a `capture_task_links` row, lineage generation 0.
2. 23 of them have a redrive child created 2026-09-06 and cancelled three minutes
   later, because a redrive then did not carry the capture link and could not reach
   the worker (`docs/research/2026-09-06-a-redrive-that-cannot-reach-the-worker.md`,
   fixed 2026-09-07). Inserting those children raised the originals'
   `lineage_generation` to 1, and `_require_redrivable` read that as "redriven once",
   so the queue's own `redrive` refuses them (`redrive_generations_exhausted`).
   `lineage_generation` counts every change to a lineage and is the version the
   lineage compare-and-set reads; it was never a count of chances the worker got.
3. The causes of their deaths (the stray pre-adoption candidate, the writer race)
   were fixed after they died. Nothing redrives a dead task automatically.
4. `flush_memory._session_time` files a capture under the day of the session when
   that day is within `MAX_BACKDATED_CAPTURE_DAYS` (30); the oldest of the 25 is 29
   days old today.
5. `_new_ordinary_purge_plan` collects capture evidence for every selected row;
   `_require_capture_terminal_proof` raises `capture_intent_unresolved` for a row
   without `run/queue-results/capture-<intent>.json`, and the whole plan aborts. No
   test covers a dead capture task in a purge.

## Decision (conclusion)

- **A dead capture gets one automatic redrive once the code has changed after it
  died.** A fix arriving is the event the field waits for before a redrive; the
  nightly pass stands in for the operator. The nightly redrives every dead capture
  task at lineage depth 0 whose death (`updated_at`) is older than the checkout's
  HEAD commit time, at most 50 a night, before the queue worker runs. A redriven
  task that dies again is final: it is retained and doctor names it.
- **A redrive is spent only when its child could reach the worker**: the child
  carries the parent's capture link, or the parent was not a capture. The 23
  children of 2026-09-06 carried no link and did not spend the chance.
  `_require_redrivable` stops reading `lineage_generation`.
- **The purge takes what it can prove and keeps the rest.** A row whose capture has
  no terminal record is left out of the plan instead of aborting it, and the
  receipt names it as retained.
- The docstring of `_ordinary_purge_selection` and the user guide say what the
  weekly does with dead work.

## Edited files

- `scripts/memory_queue.py`, `scripts/scheduled_nightly.py`
- `tests/test_a_dead_capture_gets_its_second_chance_after_a_fix.py` (new),
  `tests/test_a_redrive_carries_the_intent_it_was_for.py`, `tests/test_memory_queue_cli.py`
- `docs/USER-GUIDE.md`

## Uncertainty

A capture older than 30 days at redrive time is filed under the day it is
processed, as `flush_memory` already does for any late capture.
