# A lock lives as long as its process, not thirty minutes

Date: 2026-09-10. Trigger: audit finding OPS-01 (with OPS-11 and H4 of the
same day), reproduced by the auditor: a compile lock whose process is alive
and whose timestamp is 31 minutes old makes `maybe_compile.status()` report
`compile_running: False`, `_clear_lock()` refuse without saying so, and
`spawn_compile_if_idle()` answer `skipped: lock race lost` with exit 0. The
nightly's wait then ends at the same second as the stale-by-age verdict and
lint, backlink repair, the FTS rebuild and the generation refresh run on top
of a live compile. One compile of one daily log measured 6.5 minutes through
the Claude CLI (issue #21); a backlog crosses thirty minutes.

## Sources

1. PostgreSQL `src/backend/utils/init/miscinit.c`, `CreateLockFile`: a
   `postmaster.pid` left behind is stale when `kill(pid, 0)` says the process
   is gone (`ESRCH`), or when the PID is our own or an ancestor's; the file's
   age is never consulted. The one extra check is shared memory still in use
   by orphaned backends — evidence of life, not of time.
   https://github.com/postgres/postgres/blob/master/src/backend/utils/init/miscinit.c
2. `flock(2)`: a lock is released by an explicit `LOCK_UN` or when every
   descriptor holding it is closed — which the kernel does when the process
   exits. There is no time-based expiry.
   https://man7.org/linux/man-pages/man2/flock.2.html
3. This repository's own fence, `scripts/operational_ownership.py`: the V3
   registry decides liveness by process start identity (PID reuse safe) and a
   heartbeat-refreshed lease; an expired owner is reclaimed only when its
   process is provably dead (`_reclaim_or_refuse`: "doubt refuses"). The
   compile role, marker `run/compile.pid`, and `acquire_compile_owner` exist
   and are tested, and no production compile takes them yet (OPS-02, OPS-08).

## Findings

**Age is not evidence.** Both external precedents and the repository's own
registry decide "running" by the process, never by the clock. The legacy
compile lock added a second verdict, `age > 30 min → stale`, which contradicts
the first whenever a compile is merely long. Two readers of the same file
(`_is_compile_running`, `_clear_lock`) each implemented both verdicts with
different outcomes: the first said "not running", the second refused to
clear. That is why the reported reason was a race that did not happen.

**A wait bound is not a liveness bound.** `scheduled_nightly.COMPILE_WAIT_SECONDS`
is how long a nightly pass is willing to follow a compile before deferring
the steps that read its output. It stays: deferring is honest. What must go
is the lock's own opinion that thirty minutes means dead.

**A lock error is doubt, and doubt refuses.** `compile_memory._acquire_compile_lock`
returned `False` ("the spawner owns it, proceed") on any exception, so a
missing module or an unreadable lock let two compiles write the same daily
log; a failed release was swallowed. The registry's rule applies here too.

**The exit code read the reason text.** `maybe_compile.main` returned 0 when
the word "skipped" appeared in the reason (OPS-11). The outcome is now a
value the function returns, and the text is for people.

## Decision

1. One predicate, `maybe_compile._lock_state()`, answers `absent`, `stale`
   or `live` with its reason; `_is_compile_running` and `_clear_lock` both
   read it. A live process holds its lock however old the file is. Stale
   means: the process is dead, the PID-0 placeholder outlived its 10 s
   spawn window, or the file cannot be parsed. `MAX_COMPILE_DURATION_S` is
   deleted.
2. `spawn_compile_if_idle` reports the lock's real state after a lost claim
   ("running pid=… since …" or "spawning since …"), never "lock race lost".
   `main` decides its exit code from the returned outcome, not from text.
3. `compile_memory` refuses to run when the lock cannot be taken or read,
   prints the reason, and reports a failed release to stderr.
4. The canonical registry (source 3) is the next step for the compile and
   the nightly: task 14 (OPS-02/03). This note does not change ownership;
   it removes the false verdict from the legacy lock the product runs on.

Files: `scripts/maybe_compile.py`, `scripts/compile_memory.py`,
`tests/test_maybe_compile.py`, `tests/test_compile_failure.py`,
`CHANGELOG.md`, `docs/ISSUES-2026-09-10.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.

## What this does not settle

A compile whose process is alive but wedged holds the lock until it dies;
`status()` names its PID and start time, and the provider ceiling
(`COMPILE_PROVIDER_CEILING_S`, 300 s per call) bounds each call. Whether the
doctor should raise a finding for a compile running longer than a stated
budget is a separate question for the ownership work in task 14.
