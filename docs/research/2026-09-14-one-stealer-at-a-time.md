# One stealer at a time

Dated 2026-09-14. Item 2.5 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `run/state.json` is guarded by an `O_CREAT|O_EXCL` lock file holding the owner's PID
  (`memory_state._claim_lock`). A lock older than `_STALE_LOCK_SECONDS` whose owner is
  dead is retired by `retire_stale_lock(path, judged)`: rename the file aside, compare
  the moved bytes with the bytes the caller judged, delete on a match, and on a
  mismatch put the file back with `os.link`
  (`docs/research/2026-09-10-a-stale-lock-is-moved-aside-and-checked-before-it-is-removed.md`).
- The audit reproduced two holders with a forced order: stealers A and B both judge the
  dead lock X. A retires X and creates its own lock. B renames A's fresh lock aside,
  sees it is not X, and tries to put it back — but C has created a lock in the
  meantime, so the link fails, A's lock stays aside, and A and C both believe they hold
  the lock.
- The flaw is that the rename happens before the check: a stealer moves a file it has
  not yet verified. Between a caller's judgement and its rename, other stealers and
  creators run.
- `retire_stale_lock` has three callers (code graph and `grep`):
  `memory_state._await_lock_turn` (the state lock), `maybe_compile` (the compile lock)
  and `scheduled_nightly` (the legacy maintenance marker). All three have the same
  shape of race.

## Practice on this date

- A check and the action it justifies must be atomic with respect to every other
  process that can invalidate the check (time-of-check to time-of-use, CWE-367,
  [MITRE](https://cwe.mitre.org/data/definitions/367.html)).
- Serialise the rare, dangerous path with an OS lock the kernel releases when its holder
  dies — `flock(2)` on POSIX, `LockFileEx` via `msvcrt.locking` on Windows — rather than
  inventing more rename choreography; `scripts/claims.py` already uses exactly this pair
  for its rebuild lock.

## The decision

- `retire_stale_lock` runs under a steal guard: an exclusive OS lock on a sidecar file
  `<lock>.steal` (`fcntl.flock` / `msvcrt.locking`, waited for a bounded time). Inside it,
  it reads the current bytes first and does nothing unless they still equal the judged
  bytes; only then does it rename and delete.
- Why that closes the race: while the judged file exists, no creator can create a lock
  (`O_EXCL`), its dead owner cannot release it, and every other stealer waits for the
  guard. So the file read inside the guard is the file renamed. The put-back step is no
  longer needed and goes.
- All three callers get the guard, because it lives in the one function they share.
- A process still running code from before this change steals without the guard; the
  race window with it is the one that existed before, and it closes once every process
  runs the new code.

Files: `scripts/memory_state.py`, `tests/test_memory_state_permissions.py`,
`tests/test_one_stealer_at_a_time.py`, `docs/research/2026-09-14-one-stealer-at-a-time.md`.
