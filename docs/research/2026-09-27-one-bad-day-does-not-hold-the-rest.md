# One bad day does not hold the rest

Date: 2026-09-27. Audit 2026-09-27, finding A-3 (A-1 of 2026-09-26 fixed one
instance, not the class).

## What was wrong

`compile_memory._run` returned on the first batch with a non-zero status. Dailies are
selected oldest first, and `select_dailies` does not look at recorded source failures,
so one day that cannot compile was tried first on every run and every newer day waited
behind it. Live fact: the last successful compile was 2026-09-25 11:31 UTC; every run
since failed on the same day.

## Alternatives considered

1. Skip days with a recorded failure until their content changes. Rejected for now:
   a transient provider failure would then block a day for good, and choosing a
   back-off period is a limit with no measured basis (law 9).
2. Stop at the first failure (the old behaviour). Rejected: it turns one bad input
   into a stall of the whole pipeline.
3. Record the failure and run the remaining batches; exit 1 at the end. Chosen. Each
   batch is resolved against its own refreshed snapshot, so batches are independent;
   the failed day keeps its source-failure record and stays pending for the next run.

What this does not solve: the failed day is still tried on every run (its provider
calls repeat). That cost stays visible as a failed batch and a source failure; a
back-off needs a measured basis first.

## Decision

The batch loop appends every outcome; `_finish_run` exits 1 when any batch failed
(its failure already recorded by `_failed_compile`) and marks the run ok otherwise.
Guard: `tests/test_one_bad_day_does_not_hold_the_rest.py` — an older day whose plan
fails no longer stops the newer day from compiling; the run still exits 1. It fails
on the previous code.

## Sources (fetched 2026-09-27)

- Enterprise Integration Patterns, Dead Letter Channel,
  https://www.enterpriseintegrationpatterns.com/patterns/messaging/DeadLetterChannel.html —
  "When a messaging system determines that it cannot or should not deliver a message,
  it may elect to move the message to a Dead Letter Channel."
- Amazon SQS Developer Guide, dead-letter queues,
  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html —
  "DLQs are useful for debugging your application because you can isolate unconsumed
  messages to determine why processing did not succeed."
- Microsoft Azure Architecture Center, Bulkhead pattern,
  https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead — "Isolate
  the elements of an application into pools so that if one element fails, the others
  continue to function."

Conclusion (mine): all three isolate the unit that fails so the rest keeps flowing,
while keeping the failed unit visible for diagnosis — here the source-failure record.

## Files

- `scripts/compile_memory.py`
- `tests/test_one_bad_day_does_not_hold_the_rest.py`

## Follow-up qualification, 2026-09-29

The independent-batch loop still called `_mark_finished` from `_failed_compile`.
That changed the whole run to `error` and removed its PID lock while later batches
were still running. A real pass stopped waiting for the compiler at its first
validation failure; the compiler continued publishing afterwards without its lock.
The missing lock, live process, scheduler timestamps and call chain distinguish
this from a slow provider or an expired wait budget.

Each failed batch now records its own durable source failure and carries its error
in `BatchOutcome`. Only the end of the batch loop records the aggregate failure and
releases the run lock. Packing failure and process-level exceptions still finish
the entire run. A dry run records no source failure. Receipts and validation are
unchanged, so resumed runs retain committed parts and retry pending parts.

Reviewed primary references on 2026-09-29:

- [Python fcntl](https://docs.python.org/3/library/fcntl.html): lock acquisition,
  release and contention are distinct operations; an arbitrary exception is not
  evidence that another owner holds a lock.
- [Linux flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html): ownership
  lifetime and explicit release govern exclusion. This is an analogy for the
  existing portable PID/token lock, not a claim that it is a kernel flock.
- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html): transaction
  boundaries and lock lifetime are part of concurrency correctness; the product
  continues to use its existing rollback-journal coordination contract.

Alternatives: stopping at the first failed batch regresses independent progress;
reacquiring the lock after each failure introduces an exclusion gap; replacing the
cross-platform lock adds unrelated migration and compatibility work. Retaining
one owner until the run ends is the smallest correction of the existing contract.
No new dependency, retry limit, path, environment contract or runtime component.

Regressions distinguish the old and corrected behavior using a real PID lock and
run state, and verify aggregate failure after mixed failed/successful outcomes.
Existing resumed-receipt, cancellation, MCP-entry and scheduler tests remain gates.
