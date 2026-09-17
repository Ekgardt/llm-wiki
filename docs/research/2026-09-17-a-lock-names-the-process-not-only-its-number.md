# A lock names the process, not only its number

Date: 2026-09-17. Trigger: third audit, findings Q-L5, Q-L6, Q-L7, Q-L8 and
M-B6, all left in round one as "needs a format change". The owner delegated
the decision; this note records it.

## What was found (each verified by reading the code, L5 and L6 by a run)

- **Q-L5.** `operational_ownership._timestamp` writes
  `datetime.isoformat()`, which drops the fraction when the microsecond is
  zero. The registry then compares `expires_at > ?` as text in SQL. `Z` sorts
  after `.`, so `…:00Z` compares greater than `…:00.500000Z`: a lease written
  on a whole second is judged up to a second wrong. The scheduled owners
  freeze their clock on a whole second, so the two forms do meet in one table.
- **Q-L6.** `memory_state._await_lock_turn` asks whether the owner of
  `state.json.lock` is alive only once the file is 30 s old. A writer waits
  10 s. So a writer that died holding the lock stops every other writer for
  30 s, and each of them times out first.
- **Q-L7.** `state.json.lock`, `run/maintenance.lock` and `run/compile.pid`
  name their owner by PID alone. The note of 2026-09-11 already said it: "a
  PID-only marker cannot be made reuse-safe by any probe". A reused PID holds
  the lock for as long as the unrelated process lives.
- **Q-L8.** On macOS `proc_pidinfo(PROC_PIDTBSDINFO)` is called with `arg=0`,
  which does not find a zombie; `kill(pid, 0)` then succeeds, the probe raises,
  the state is `unknown`, and the lease of a dead-but-unreaped owner can never
  be reclaimed. Linux already reads state `Z` as gone.
- **M-B6.** The compile lock: (1) PID reuse as in L7; (2) a spawned compile
  that reaches the lock before its spawner has replaced the PID-0 placeholder
  refuses itself; (3) a lock file created and not yet written (the fallback for
  filesystems without hard links) reads as "unreadable" and is removed.

## Sources

1. Python `datetime.isoformat`: "`YYYY-MM-DDTHH:MM:SS.ffffff`, if microsecond
   is not 0; `YYYY-MM-DDTHH:MM:SS`, if microsecond is 0", and
   `timespec='microseconds'`: "Include full time in `HH:MM:SS.ffffff` format."
   https://docs.python.org/3/library/datetime.html (fetched 2026-09-17)
2. XNU `bsd/kern/proc_info.c`, `proc_pidinfo`: for `PROC_PIDTBSDINFO`
   "`if (arg) { findzomb = 1; }`", then `proc_find_zombref(pid)`; and
   `proc_pidbsdinfo` sets `pbsd->pbi_status = p->p_stat;`. `bsd/sys/proc_info.h`
   documents the status as "p_stat value, SZOMB, SRUN, etc"; `SZOMB` is 5 in
   `bsd/sys/proc.h`. https://github.com/apple-oss-distributions/xnu (fetched
   2026-09-17)
3. `kill(2)`: "Note that an existing process might be a zombie, a process that
   has terminated execution, but has not yet been wait(2)ed for."
   https://man7.org/linux/man-pages/man2/kill.2.html (fetched 2026-09-17)
4. This repository: `operational_ownership.process_start_identity` (boot id
   and start ticks on Linux, start time on macOS, creation FILETIME on
   Windows) is the reuse-safe identity the registry already stores;
   `blackboard._timestamp` already writes `timespec="microseconds"`.
   PostgreSQL's `postmaster.pid` carries the start time beside the PID for the
   same reason.

## Decision

1. **One timestamp shape.** `_timestamp` always writes six fractional digits.
   Text order then equals time order. The reader is unchanged
   (`datetime.fromisoformat` reads both shapes), so rows written before the
   change stay readable; they are leases of 30–120 s and are gone within
   minutes of the upgrade.
2. **A lock file names the process.** The owner line is followed by one more
   line: the process start identity. The probes move, unchanged, from
   `operational_ownership` into the dependency-free `process_liveness`
   (importing `operational_ownership` costs a hook 0.14 s; `process_liveness`
   0.01 s), and `operational_ownership` keeps its names. One function,
   `process_liveness.owner_alive(pid, start_identity)`, answers for every
   lock: with an identity it compares identities (a reused PID is dead);
   without one — a file written by the previous release — it falls back to the
   PID probe. Doubt stays alive. Format per file:
   `state.json.lock` `pid\nidentity\n`; `maintenance.lock` the same;
   `compile.pid` gains a fourth line. Every reader accepts the old shape.
3. **A named dead owner is retired at once.** A complete new-format state lock
   (it ends in a newline, so it is not a half-written file) whose owner is
   provably dead is retired without waiting for the 30 s age. The age rule
   stays for anything else: an empty, partial or old-format file.
4. **macOS.** `proc_pidinfo` is called with `arg=1`; status `SZOMB` reads as a
   missing process, exactly as Linux state `Z` does.
5. **Compile lock.** The spawner hands its placeholder token to the child
   (`--lock-token`) and keeps that token when it writes the child's PID, so the
   child recognises its lock whether it sees PID 0 or its own PID. A lock file
   that is empty and younger than the 10 s spawn window is a lock being
   written, not an unreadable one.
6. **Not changed, by decision.** The queue's `source_fences` delete a fence
   when it is expired *or* its PID is dead, so a reused PID holds a fence no
   longer than its lease — not a defect. The legacy v2 `queue_ownership` row is
   PID-only, but it lives in the pre-adoption database that an adopted vault
   replaces with a tombstone, and every install now adopts; adding a column to
   a retired schema is not worth a migration.

Not run on Windows or macOS: the probes moved verbatim, the macOS change is
covered by a simulated `proc_pidinfo`.

Files: `scripts/process_liveness.py`, `scripts/operational_ownership.py`,
`scripts/memory_state.py`, `scripts/maybe_compile.py`,
`scripts/compile_memory.py`, `scripts/scheduled_nightly.py`,
`scripts/installed_memory_repair.py`, `tests/test_process_liveness.py`,
`tests/test_operational_ownership.py`,
`tests/test_a_lock_names_the_process_not_only_its_number.py`,
`tests/test_a_lease_expiry_sorts_as_time.py`,
`tests/test_a_spawned_compile_knows_its_lock.py`.
