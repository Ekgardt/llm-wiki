# A dead task counts until it is resolved

Date: 2026-09-27. Audit 2026-09-27, finding B-13 (my B-23 fix of 2026-09-26 hid what
it was written to show).

## What was wrong (live, read-only)

Doctor counted only dead queue tasks that died within 7 days. All 25 live dead tasks
died 08-27..09-08, so the queue read healthy while 25 captures stayed unresolved. The
7-day window assumed the weekly purge exports older ones — but the weekly had not run
since 09-13 (B-15).

## Decision

Every dead task in the queue counts (`dead_unresolved`) until its redrive succeeds or the
weekly purge exports it past the retention window; the age of the oldest is reported
(`oldest_dead_days`) and never used to hide. `DEAD_TASK_LIVE_SECONDS` and the
recent-only helper are removed.

## Sources (fetched 2026-09-27)

- Amazon SQS Developer Guide, dead-letter queues,
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html —
  "DLQs are useful for debugging your application because you can isolate unconsumed
  messages to determine why processing did not succeed."
- Enterprise Integration Patterns, Dead Letter Channel,
  https://www.enterpriseintegrationpatterns.com/patterns/messaging/DeadLetterChannel.html —
  "When a messaging system determines that it cannot or should not deliver a message,
  it may elect to move the message to a Dead Letter Channel."
- Google SRE book, Monitoring Distributed Systems,
  https://sre.google/sre-book/monitoring-distributed-systems/ — "Your monitoring system
  should address two questions: what's broken, and why?"

Conclusion (mine): a dead letter is kept to be looked at; its age says how long it has
waited, not that it stopped mattering.

## Guard

`tests/test_a_failure_in_the_night_is_named.py::test_every_dead_task_counts_and_the_oldest_age_is_shown`
— a 1-day and a 30-day dead task both count, oldest 30 days (the previous code counted 1).

## Files

- `scripts/doctor.py`, `tests/test_a_failure_in_the_night_is_named.py`

## Follow-up, 2026-09-29

Rechecked the three primary sources above. The implementation still counted
retained dead parents after successful redrives. Sixteen real parents had
successful children with matching kind/input hash and intact result digests.
Their retained rows describe history, not unresolved work.

Doctor now follows explicit redrive ancestry only after validating the successful
child's result bytes against its stored digest. Each link must retain the same
kind and input hash. Pending, changed-input, missing-result and corrupt-result
retries do not resolve a parent. No queue state, history or retention policy is
changed; age still cannot hide unresolved work. Only retry candidates need result
reads, avoiding a second read of every unrelated successful result.

Deleting history, ignoring all old failures, or accepting an uncompleted retry
would remove evidence or hide failures. The selected read-only interpretation
uses existing queue identities without a new state format. Its conservative
tradeoff is that missing ancestry or result evidence leaves work unresolved.
The regression fixture covers a retry chain, unrelated dead work, an unfinished
retry, changed input and a corrupt digest.

Capture counters similarly count failure events, including repeated attempts;
they cannot establish the number of unique lost captures. Doctor and session
summaries now say that explicitly. The doctor detail is named `failure_events`
instead of the misleading `lost`; the persisted diagnostic format is unchanged.
