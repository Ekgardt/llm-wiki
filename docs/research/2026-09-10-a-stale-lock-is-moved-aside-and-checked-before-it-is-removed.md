# A stale lock is moved aside and checked before it is removed

Date: 2026-09-10. Trigger: audit finding OPS-07. Three legacy lock stealers
decide "stale" from one read and then `unlink` the path: `maybe_compile._clear_lock`
(`run/compile.pid`), `memory_state._await_lock_turn` (`run/state.json.lock`)
and `scheduled_nightly._steal_marker` (`run/maintenance.lock` on a vault
without a V3 coordinator). Between the decision and the unlink another
process may have removed the dead lock and claimed a fresh one; the second
stealer then unlinks a live lock and both proceed — for `state.json.lock`
that is two read-modify-write writers on the state file.

## Sources

1. POSIX `rename(2)` / Python `os.replace`: the rename is atomic and exactly
   one of several concurrent renames of the same source succeeds; the others
   fail with `ENOENT`. https://docs.python.org/3/library/os.html#os.replace
2. Python `os.link`: creating a hard link fails with `FileExistsError` when
   the target exists — an atomic "put back only if nobody else took the
   name" (POSIX, and NTFS on Windows). https://docs.python.org/3/library/os.html#os.link
3. This repository: `operational_ownership._remove_exact_marker` already
   removes a marker only after comparing its identity and content with what
   the caller recorded; `memory_state._release_state_lock` already guards
   the release by content. The steal was the one path without a guard.

## Decision

One helper, `memory_state.retire_stale_lock(path, judged)`: rename the lock
aside to a unique name (one winner among concurrent stealers), compare the
moved bytes with the bytes the caller judged stale, and delete them only
when they match; when they differ — a fresh owner's lock was moved — put it
back with `os.link` (refused if the name was taken meanwhile) and report
`False`. The three stealers pass the bytes they judged. What remains: the
three-way race in which a fresh lock is moved aside, a fourth process claims
the name before the put-back, and the put-back is refused; the moved-aside
file is left with a `.stale-` suffix as evidence and the fresh owner's
release finds its lock gone, which it already tolerates.

Files: `scripts/memory_state.py`, `scripts/maybe_compile.py`,
`scripts/scheduled_nightly.py`, `tests/test_memory_state_permissions.py`,
`tests/test_maybe_compile.py`, `tests/test_scheduled_nightly.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
