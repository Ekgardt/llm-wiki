# Bad intents do not use up the adoption bound

Dated 2026-09-17. Finding Q-M19 of the third audit (medium, confirmed by reading and here
by a test). The research before the fix.

Files: scripts/capture_adoption.py, tests/test_bad_intents_do_not_use_up_the_adoption_bound.py

## What was found

- One adoption pass asks the queue for the oldest 32 ready intents that have no task
  (`ready_capture_intents_without_task`, `ORDER BY updated_at ASC LIMIT ?`) and tries each.
  A record that cannot be adopted (its bytes moved, its file is gone) is reported as a skip
  and "left untouched" — so it is again among the oldest 32 on the next pass, and on every
  pass after that.
- Thirty-two such records therefore fill the window for good, and every newer orphan behind
  them is never examined. The bound was meant to defer work ("a bound defers work and never
  drops it", the module's own comment); here it drops it.
- Visibility: the nightly step runs `capture_adoption.py`, which prints examined, adopted
  and skipped with reasons into the nightly report. The capture worker's call in
  `flush_memory._adopt_orphaned_intents` discards the result, by its stated contract
  ("a sweeper that cannot run must never be the reason the queue is not drained"). That
  contract is kept; the nightly report is where skips are seen.
- Code graph: `adopt_orphaned_capture_intents` ← `adopt_in_active_vault` ← `main`
  (nightly step), and ← `flush_memory._adopt_orphaned_intents` ← `run_capture_worker_once`.

## Practice on this date

- This sweeper is a transactional-outbox relay (the module says so). The known failure of a
  relay that polls "oldest N unsent" is head-of-line blocking by a poison message; the
  standard remedy is to set the poison message aside so that it no longer occupies the
  head. AWS describes the same for queues: a dead-letter queue exists to "isolate
  unconsumed messages to determine why processing did not succeed"
  (https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html).
- Setting a capture intent aside durably (a new state, or moving the file) is a change to
  the Reliability v3 capture contract and to the queue's schema, which another area owns.
  The smaller remedy with the same effect on the head of the line is to not let a skip
  count against the bound.

## The decision

- The bound stays what it was written for: at most `MAX_ADOPTED_INTENTS_PER_PASS` intents
  are *adopted* in one pass. A skipped record no longer uses the bound up: the pass reads a
  window that is wider by the number of records it has skipped and goes on to the orphans
  behind them.
- The pass stays bounded: it stops after `MAX_SKIPPED_INTENTS_PER_PASS` skips (256). A skip
  is cheap next to an adoption — one bounded file read and a digest.
- Records stay untouched, as before. A durable "set aside" state is named in the report as
  the queue area's follow-up.
