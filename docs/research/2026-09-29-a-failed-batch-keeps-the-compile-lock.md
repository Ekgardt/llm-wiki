# A failed batch keeps the compile lock

Research date: 2026-09-29. Targeted correction and qualification complete;
live recovery, deployment and full audit closure remain pending.

During live recovery, the agent appended its progress page while a compiler
held an earlier snapshot of that page. The transaction correctly refused the
changed precondition; this avoidable concurrent edit wasted that batch's work.
No private knowledge edits will be made during the resumed compile.

A separate defect followed: `_failed_compile` called `_mark_finished`, which
stamped the whole run finished and removed `run/compile.pid`. `_run` nevertheless
continued to later batches. PID 1656757 was observed running after the error,
with a provider child at one observation, while the compile lock was absent.
The agent stopped only its own compiler, confirmed it had no remaining children,
and archived its output. Existing receipts and source failures were retained.

Record each failed batch against its sources and retain its diagnostic error,
but publish the overall finished state only in `_finish_run`. The initial packing
failure must also take that finalization path. Keep independent later batches
running, preserve the shared lock throughout, and return failure at the end if
any batch failed. Escaping exceptions keep the existing outer error/finally path.
This corrects the lifecycle introduced by the 2026-09-27 independent-batch change;
it adds no schema, dependency, runtime location, environment contract or limit.

Primary references rechecked today for the adjacent locking investigation:

- [PostgreSQL startup ownership](https://www.postgresql.org/docs/current/server-start.html)
  explains that a running instance's PID file prevents concurrent instances.
- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html)
  separates live access protection, failure recovery and lock release.
- [Python OS interfaces](https://docs.python.org/3.12/library/os.html#os.open)
  specify the existing file/process interfaces used by this local mechanism.

These principles support retaining ownership for the whole protected activity;
the repository's concrete call graph and reproduced missing lock establish the
defect. The APIs are available on Python 3.10; actual verification uses installed
Python 3.12.3. No newer lock library is required. Stopping all later batches would
reintroduce starvation behind a failed day. Recreating a lock after every failure
would leave a concurrency window. Suppressing the error would misreport success.
The selected change moves finalization to the existing run boundary instead.

Regression qualification must observe the lock and running status while a later
batch executes, refuse a real competing process, then report error and release
the lock when all batches have finished. Existing successful-batch receipts must
remain valid. Platform-wide and full-project qualification remain separate work.

## Verification

The new regression first failed with an absent lock, an already-finished error
state, and successful lock acquisition by a real competing subprocess while the
original run was still resolving its next batch. Its existing successful-batch
control passed. This reproduces the concurrency defect without an LLM.

The first related run after correction reported 103 passes and one failure.
An older packing test replaced `_mark_finished` with a no-op and rejected any
other diagnostic update; that setup hid the old failure status writes. It now
uses actual finalization and requires exactly the error/finish fields, no success
receipt and no successful-compile state. The provider-before-packing refusal
assertion is retained. The corrected run passed 104 tests in 38.15 seconds,
including actual complexity analysis, transaction behavior, oversized-day
isolation, competing-process locking and child-token handoff. Whole-project Ruff
and `git diff --check` passed. All failed reports are retained.

No obsolete failure finalizer remains in production: `_record_failed_batch`
records source failures and diagnostic error; `_finish_run` marks the run finished.
The initial packing-exception branch also finalizes through `_finish_run`.
Running MCP processes have not been reloaded and may retain the old function.

Doctor after stopping the compiler reports overall `error`, including four
unresolved refused transactions, with no live writer or maintenance owner.
Its aggregate codes also include historical DLP failures. A follow-up read using
the same lineage/outcome proof identifies all four unresolved records as
precondition failures in one compile operation family, not four independent
lost inputs or current DLP refusals. Nightly completion remains deferred.
These records were not discarded to make health green. Historical capture/tool
failure counters remain unchanged. Successful targeted tests do not settle them.

Evidence: `logs/audit-2026-09-29-completed-repair-batch-lock-red.txt`,
`logs/audit-2026-09-29-completed-repair-batch-lock-green.txt`,
`logs/audit-2026-09-29-completed-repair-batch-lock-green-corrected.txt`, and
`logs/audit-2026-09-29-completed-repair-batch-lock-doctor.json`.
