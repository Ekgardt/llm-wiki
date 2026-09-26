# A checkpoint that never reached the database is seen

Date: 2026-09-26. Audit 2026-09-26, finding C-12 ("checkpoint check blind before
reservation", marked as a suspicion by the audit; confirmed in the code).

## What was wrong (facts, read in the code)

A project checkpoint event first waits in `run/state.json`
(`project_checkpoint_pending`, each item with its `occurred_at`) until a drain reserves
it in `project_checkpoints`. Doctor's `_checkpoint_check` judged only the reserved rows;
the queue appeared as a depth "never the finding", and with no checkpoint database at
all the check returned `ok` before looking at the queue. A project whose events never
reached the database was reported as fine for up to 30 days, when the queue drops them
(`PENDING_EVENT_MAX_AGE`).

## Source

Amazon SQS developer guide, "Available CloudWatch metrics for Amazon SQS", fetched
2026-09-26 from
https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html:
`ApproximateAgeOfOldestMessage` — "The age of the oldest unprocessed message in the
queue." The same page's `ApproximateAgeOfOldestMessageInQuietGroups` is "Used for
monitoring SLA compliance and detecting processing bottlenecks".

Conclusion (mine): a queue's health is the age of its oldest item, not its depth; a
deep queue that moves is fine, a short one whose head never moves is not.

## Decision

- Doctor reads each project's oldest queued `occurred_at`. Older than
  `CHECKPOINT_QUEUE_STUCK_SECONDS` (36 h: the nightly reclaim drains every queue once a
  day, plus half a day for a late or skipped start), it is a stuck head like a reserved
  row, and the finding names the project.
- A missing checkpoint database no longer ends the check before the queue is read.

## Files

- `scripts/doctor.py`
- `tests/test_a_checkpoint_that_never_reached_the_database_is_seen.py`
