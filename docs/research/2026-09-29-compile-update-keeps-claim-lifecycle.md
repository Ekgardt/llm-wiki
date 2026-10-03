# A page update and its claim lifecycle share one after-image

Research date: 2026-09-29. Same-page correction implemented and component-tested;
installed failed-batch replay remains unverified.

The live manual compiler reports a claim-lifecycle/operation-target overlap.
A temporary-vault reproduction establishes one concrete cause: an authoritative
new claim updates the same page carrying the old claim it supersedes. The page
update and lifecycle planner independently schedule replacement of that path.
The existing overlap guard correctly rejects the ambiguous transaction. This
reproduction does not prove that every live overlap has this exact cause.

Selected correction: apply verified lifecycle changes to the captured original
claim ledger, then append the update and merge its new claims into that single
page image. Remove only those already-incorporated lifecycle mutations from
subsequent planning. All original claim identities, snapshot preconditions,
writer admission and transaction checks remain required. No persisted schema,
path, environment contract, dependency or runtime location changes.

Alternatives: separate commits permit partial publication; dropping the overlap
guard permits competing after-images; skipping lifecycle handling leaves an old
claim active; quarantining every ordinary update unnecessarily blocks a valid
authoritative transition. One verified image within the existing transaction
avoids these problems. Cost: a ledger transformation before the existing merge;
no model call, extra publication or new limit. Multiple independent lifecycle
writers and cross-page lifecycle changes overlapping another page's update still
require investigation. These paths retain the existing refusal. This correction
only incorporates lifecycle transitions assessed from the page's own update.

Primary sources checked today, independently maintained:

- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): the all-or-none
  transaction boundary and rollback-journal filesystem assumptions.
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html):
  concurrent changes require validated snapshots or a complete retry.
- [Git lockfile API](https://git-scm.com/docs/api-lockfile): prepare replacement
  contents under writer exclusion before publishing the file.

These principles support the project-specific design inference, not a proof of
implementation correctness. PostgreSQL and Git's lockfile API are not new runtime
dependencies. The implementation remains on the existing Python 3.10+ and
SQLite rollback-journal/FULL contract; no technology version is upgraded.

Required qualification: regression fails with the existing overlap guard;
successful publication preserves old history, supersedes the intended claim,
keeps the new claim active and writes one page operation plus a receipt. Existing
stale-identity, source drift, quarantine and transaction tests must remain green.
Actual CCN/branch-shape analysis and post-change indexing remain mandatory.

## Qualification

The actual regression failed before the correction with the same overlap error
reported by the live compiler. The first broader run passed 81 checks but failed
the real complexity gate: two new functions measured CCN 6. Their reported code
was split without changing the threshold. The corrected run passed 139 tests in
40.82 seconds across compile transactions, contradiction policy, claims and the
repository-wide Lizard/AST gate. This includes existing stale-identity and
concurrent-write refusals. Ruff passed. No production guard was disabled.

All 1101 source files in the isolated copy match the working tree. The existing
lifecycle transform was extracted and reused, not retained as a second
implementation. No extra model call or new numerical limit was introduced.
The running compiler loaded the old code before this change; its successful
later batches do not qualify this correction. Its observed backlog was 50 parts
in two days, and the failed batch still needs replay after this run releases its
lock. No private progress/log write was performed during its input snapshot.

Evidence prefix: `logs/audit-2026-09-29-completed-repair-compile-overlap-`.
`red.txt` retains the reproduced defect, `green.txt` the failed complexity run,
`corrected-green.txt` the successful checks, and `source-comparison.json` the
source comparison. This is not full audit closure or proof of every live overlap's
cause. The checked runtime is Python 3.12.3 with SQLite 3.45.1; portable syntax
retains Python 3.10 compatibility, but this run is not a Python 3.10 execution.

## Installed replay coordination

A one-off follow-up process now waits for the currently running compiler using
its Linux process descriptor (`pidfd_open` plus `poll`). The command identity
was checked before waiting. When that specific process exits, the follow-up
executes the ordinary manual compile entry point, including normal lock admission
and receipt-based selection of still-pending input. It does not kill the current
compiler, delete its lock or run two writers concurrently. This is an audit
operation on the present Linux host, not a new product daemon or platform contract.
Its log prefix is `logs/audit-2026-09-29-compile-retry-after-lifecycle-fix`.
At the last check it was still waiting; execution or success of the retry must
be verified from those logs and receipts, not inferred from scheduling it.
