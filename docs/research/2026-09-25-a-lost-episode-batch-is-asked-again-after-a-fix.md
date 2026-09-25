# A lost episode batch is asked again after a fix, and one silent batch does not stop the night

Date: 2026-09-25. Audit items B-4 and B-10 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and in the live logs)

- `llm_client.call_llm` returns `None` when no provider produced an answer and
  `""` when a provider answered with empty text. `_consolidate_batch` treated both
  as `ConsolidationUnavailable`, which ends the whole run
  (`_consolidate_reported`). Days are taken oldest first, so a batch whose prompt
  always gets an empty answer stops every later day every night. The live logs
  hold one "consolidation provider returned nothing"; which of the two it was is
  not recorded.
- A batch whose reply cannot be read is retried `MAX_BATCH_ATTEMPTS` (3) times,
  then marked done and failed. Once every batch is done the day is recorded in
  `consolidated_session_days` and `_already_consolidated` closes it for good
  unless its records change. A parser fixed later never sees that batch again,
  and the day record does not even say a batch was lost.
- The live vault has 30 consolidated days and no open progress. Batches lost
  before this change are not identifiable: a batch that yielded no lesson writes
  no marker either. This fix works from now on.

## Source

- Amazon SQS dead-letter queues,
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
  (fetched for the dead-capture note of 2026-09-25): failed messages are kept
  apart and moved back once the cause is fixed. The same rule was applied to
  dead capture tasks on 2026-09-25
  (`docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`).

## Decision

- B-4: `None` from the provider stops the run as before; an empty answer is a
  failure of that batch, counted by `failed_once`, and the run goes on.
- B-10: a day closed with failed batches records their keys and the code
  revision it was closed under (`git rev-parse HEAD` of the vault). When the
  revision differs, the day is open again; finished batches are found by their
  markers and not asked again, so only the lost ones cost a call. A revision that
  cannot be read changes nothing.

## Files

- `scripts/episode_consolidation.py`
- `tests/test_a_lost_episode_batch_is_asked_again_after_a_fix.py`
- `CHANGELOG.md`
