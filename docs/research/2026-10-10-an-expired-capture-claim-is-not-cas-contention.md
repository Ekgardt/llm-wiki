# An expired capture claim is not CAS contention

Research date: 2026-10-10. Python compatibility floor: 3.10. The installed
diagnostic environment uses Python 3.12 and SQLite 3.45.1.

## What was observed

After the operating system killed a full compile under global memory pressure,
its normal resume timed out waiting for the global Markdown writer. A live
capture worker held that writer while creating thousands of quarantined append
candidates. Its capture fence had expired; its binding identities still matched.
Checking the actual persisted capture preconditions through a read-only connection
raised `precondition_failed`. This is a separate failure from the compiler's OOM.
The reason the capture heartbeat initially stopped remains unqualified.

The common append loop classified `precondition_failed` as an opportunity to
advance to a fresh CAS candidate. Advancing also reset its stall clock. A new
target image can resolve a target CAS conflict, but cannot renew the immutable
claim supplied by that caller. Repeating the latter held the writer indefinitely.

## Research and choice

Three independent primary sources were checked on the research date:

- [Microsoft's retry pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry)
  distinguishes transient failures from failures that should immediately return
  to the caller, and recommends retrying where the full operation context is known.
- [AWS on timeouts and retries](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter)
  explains how retries consume resources and amplify overload and side effects.
- [Google Cloud's retry strategy](https://docs.cloud.google.com/storage/docs/retry-strategy)
  distinguishes permanent failures and conditional idempotency, and warns against
  endless retries of persistent errors.

These are general retry principles, not claims that cloud APIs implement this
local transaction contract. Applying them here means checking the caller's
existing extra preconditions before advancing a refused append. Valid target CAS
conflicts retain the existing retry behavior. A lost claim or changed immutable
source returns the existing failure to the durable queue's existing policy.

Increasing timeouts or adding an arbitrary candidate cap would leave the cause
intact. Bypassing the fence would weaken capture safety. An additional SQL index
was also rejected: the exact recovery query on a consistent diagnostic snapshot
of more than 274,000 transactions took about 0.107 seconds and returned one row.
Repeated invocation was the multiplier; one scan was not a minutes-long blocker.

The change adds no schema, runtime directory, environment variable, daemon,
dependency, or attempt limit. It adds one persisted-precondition read on an
advance with caller-supplied extra preconditions. Appends without those conditions
do not open an additional connection. The tradeoff is that callers must acquire
a new valid claim rather than retry under an invalid one; durable evidence is kept.

## Qualification and limits

The causal tests run real queue, capture and Markdown transaction operations on
temporary vaults. Faults expire a capture fence, remove its binding projection,
or change a source after preparation. Test-only cancellation stops the original
endless loop after two actual candidates; it does not change production policy.
The original implementation fails all three refusal tests. The candidate passes
those tests and the control case: a target CAS conflict retries successfully while
the source precondition still holds. All four pass on Python 3.10 as well.

Lizard and AST inspection measure every changed or new function, including nested
test functions: maximum CCN 5, at most two `if` statements, and branching/loop
nesting at most two. Existing historical-CAS and queue/capture checks are also run.
This is not evidence of completed installed compilation, full CI, or whole-cycle
memory-pressure qualification. Those remain separate operational checks.

No old implementation remains alongside the guard. Quarantined attempts, durable
capture parts and queue results are retained under the existing recovery contract;
they are evidence, not disposable legacy artifacts.
