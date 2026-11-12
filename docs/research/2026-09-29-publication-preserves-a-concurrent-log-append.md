# Publication preserves a concurrent log append

Research date: 2026-09-29. Implementation qualification is recorded below; this
document does not certify the whole audit complete.

The live nightly failed with `PreconditionChangedError` for `knowledge/log.local.md`
after an agent appended a progress entry while compilation was in flight. The
compiler snapshots the editorial log before model work, then uses that old body
and digest when appending its own completion entry. Re-instantiating `_ApplyPlan`
keeps the same `CompileInputs`, so repeating publication does not refresh the log.
The refusal prevents data loss, but the normal concurrent append cannot complete.

The connected Codebase Memory graph and source inspection show the same problem
in the generated index: publication renders its base from old note snapshots and
uses the old index digest. A newly published unrelated note can be omitted, or a
concurrent index rebuild can cause another refusal. Neither derived index bytes
nor the editorial log should be rewritten from an outdated before-image.

Chosen correction: after acquiring the existing writer gate, read the current
index/log before-images, render the index from current notes plus the validated
pending changes, and append the completion entry to the current editorial log.
Prepare/apply still validates the just-read target hashes. Keep the original
model inputs, evidence digests, target-note preconditions, claim-tree assessment,
receipts and transaction protocol. The editorial entry says which source snapshot
was compiled; it does not assert that later source changes were reviewed.

Alternatives: holding the writer gate across model calls would block all capture
and violate the established external-work rule; overwriting or removing hash
checks would lose concurrent data; rerunning the model solely because an editorial
entry was appended would add cost without changing the validated semantic plan.
Only deterministic publication metadata is refreshed. A changed semantic target
must still be refused, and an external editor racing after the refreshed read
must still fail the hash check.

Primary sources consulted on 2026-09-29:

- [SQLite isolation](https://www.sqlite.org/isolation.html): writer serialization
  and why a stale read view cannot simply be promoted into a current write.
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html):
  lost/concurrent update concerns and the distinction between snapshot reads and
  current update evaluation. PostgreSQL is not an added dependency.
- [Git update-ref](https://git-scm.com/docs/git-update-ref): conditional updates
  verify an expected prior value; they do not justify ignoring a mismatch.

These principles support the correction; SQLite isolation alone does not make
several Markdown files atomic. The existing recoverable transaction is still
responsible for that. Runtime remains Python 3.12.3 / SQLite 3.45.1 here, with
Python 3.10 compatibility, rollback journaling and FULL synchronization retained.
No path, schema, environment variable, runtime location or dependency changes.

Required regression proof: append a separate log entry after snapshot and verify
both entries survive; add an unrelated note and rebuild the index after snapshot
and verify both notes remain indexed; preserve all semantic-target refusal,
receipt, exact-source-snapshot, concurrent-writer and log-rotation regressions.
The new tests must fail before the correction. Run the local CCN/shape gate,
related compiler/transaction tests and a real maintenance pass afterwards.

The two initial regressions failed before the correction. The corrected code
passed 96 transaction/hardening/log-rotation tests and 111 related compiler,
provider, cache and evidence checks. Two additional cases preserve the refusal
when an external editor changes the freshly read index or log before prepare.
Local measurements give CCN 2 for `_append_index_and_log` and 4 for
`_publication_sources`; the portable branch-shape checks also pass. The old
snapshot-derived publication helper was removed after checking callers.

The installed maintenance run that began at 12:43:23 UTC on 2026-09-29 recorded
a committed compile at 13:02:25 UTC. The later service/kernel check establishes
that the kernel killed the compiler at 13:05:22 UTC during global memory
exhaustion; the state file's remaining `running` value is not process-liveness
evidence. That commit confirms publication in the working vault, but the full
maintenance cycle failed. The simultaneously running verification suite and
Codebase Memory indexer used about 1,970 MiB and 1,342 MiB respectively in the
kernel's OOM snapshot. Isolating their filesystem did not isolate their memory
use. Subsequent heavyweight verification and real maintenance must run
sequentially; no OOM protection or resource check is disabled to obtain success.

A subsequent sequential nightly run (13:15:33–13:45:44 UTC) published batches,
but hit the configured 1,800-second compile wait. It deferred lint/index/graph,
and systemd stopped the unfinished child when the parent exited. The parent
exit code zero does not certify maintenance completion. See
`2026-09-29-deferred-maintenance-is-not-complete.md` for the separate correction
to the health check that had misleadingly reported this pass as current.
