# Direct compile recovers a dead owner

Research date: 2026-09-29. The targeted correction is implemented and verified;
full-project qualification and completion of live maintenance remain pending.

The installed nightly service stopped its unfinished compiler after the compile
wait expired. A subsequent direct `compile_memory.py --trigger manual` returned
1: `lock held by another compile (stale lock (pid 1629317 dead))`. The ordinary
`maybe_compile.py` entry recovered that same lock and started PID 1656757. The
direct and MCP paths call `_try_claim_lock`, bypassing the existing recovery in
`_claim_lock`. Codebase Memory connected queries and source inspection confirm
both paths reach `_compile_under_lock`.

Use the existing shared claim/recovery function for direct and MCP entry. Keep
the existing process-identity check, byte comparison under the steal guard,
exclusive creation, and spawned-child token handoff. Do not delete a live or
unknown owner's lock, create another lock protocol, add retries, or expire a
named live owner by age. Inspection also found that `_judged_state` calls an
unparseable empty file stale while `_lock_state` protects it during the existing
fallback-write window. Before reusing recovery, make those readers agree; test
both the shared cleanup and direct entry. No new numeric bound is introduced.

Three independent primary references were checked on this date:

- [Python 3.12 OS interfaces](https://docs.python.org/3.12/library/os.html#os.open)
  document exclusive-create flags and filesystem errors. The installed runtime
  is 3.12.3; current 3.12 documentation is 3.12.14. The existing operations are
  also available on the project's Python 3.10 floor.
- [PostgreSQL 18 startup](https://www.postgresql.org/docs/current/server-start.html)
  describes PID-file ownership preventing concurrent instances and warns that
  startup recovery can outlast service timeouts. This is supporting comparison,
  not a proposal to install PostgreSQL or change this service's timeout.
- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html)
  separates crash recovery from active ownership and requires exclusive access
  before recovery. This remains relevant to this project's rollback-mode local
  stores; it is not a proof of the Python PID-file algorithm.

The Open Group pages for `open` and `link` could not be fetched (internal error
and HTTP 403 respectively); they are not counted as consulted evidence.

Manual unlinking would bypass ownership and race checks. Requiring callers to
launch a separate background trigger leaves the MCP/direct defect in place.
Replacing all locking with a new mechanism is unnecessary for this missing
call to existing recovery. The selected correction preserves conservative
refusal when ownership cannot be disproved. Live Windows/macOS behavior remains
subject to platform qualification. No paths, environment contracts, dependencies,
runtime locations, model calls, or schema versions change.

## Verification

Before the correction, the new tests produced four failures and eight passes:
direct recovery failed for a real reaped subprocess and an expired empty
placeholder; cleanup incorrectly removed each of two fresh empty/whitespace
placeholders. Live/unknown owner controls and the existing child-token checks
passed. With the correction, 62 tests passed in 32.44 seconds, covering those
scenarios, compile failure handling, background spawning, MCP locking, concurrent
stealers, process identity, atomic marker publication and the repository's actual
Lizard/AST complexity gate. Ruff and `git diff --check` passed.

Tests ran in a source copy so the live compiler could continue writing private
knowledge without violating the test suite's no-live-vault-mutation guard.
The source comparison is recorded separately; these are Linux/Python 3.12.3
results, not a Windows/macOS execution claim. The existing recovery mechanism
remains shared; no replaced implementation or transitional switch was retained.
The running compiler started before this code change and does not qualify the
new direct entry by itself. Its successful spawn is not completed compilation.

Evidence: `logs/audit-2026-09-29-manual-compile-continuation.err.log`,
`logs/audit-2026-09-29-compile-recovery-start.log`,
`logs/audit-2026-09-29-completed-repair-direct-compile-lock-red.txt`,
`logs/audit-2026-09-29-completed-repair-direct-compile-lock-green.txt`, and
`logs/audit-2026-09-29-completed-repair-direct-lock-source-comparison.json`.
