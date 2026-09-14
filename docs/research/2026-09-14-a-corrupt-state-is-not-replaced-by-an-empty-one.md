# A corrupt state file is not replaced by an empty one

Dated 2026-09-14. Item 2.6 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `memory_state.load_state` returns `{}` when `run/state.json` does not parse, after
  copying the bad bytes to `state.json.corrupt` and logging a line.
- `memory_state.update_state` — the one read-modify-write path, under the state lock —
  calls `load_state`, applies its mutator to that `{}` and saves it. The first writer
  after the corruption therefore replaces the whole state with one key. Lost with it,
  among others: `consolidated_session_days` (so every past day is consolidated again,
  and before today's fix 2.3 every one of them wrote duplicate lessons), the compile
  and nightly timestamps doctor reads, and the capture checkpoints.
- Every save goes through `atomic_write` (staged file, `fsync`, checked publication),
  so a torn write by this code is not the expected cause; an external edit, a crash of
  the filesystem, or a tool that truncates the file is.
- The code graph: `load_state` has 17 callers; 16 only read (session start, doctor,
  MCP status, maybe_compile …) and are fine with `{}`. The one that writes is
  `update_state`, used by every state writer.

## Practice on this date

- A reader may degrade to empty; a writer must not turn "unreadable" into "empty":
  SQLite's rollback journal and PostgreSQL's WAL both refuse to write over a page they
  cannot verify, and recover from the last durable image instead
  ([SQLite, atomic commit](https://www.sqlite.org/atomiccommit.html)).
- Keeping the previous version of a small file costs nothing on filesystems with hard
  links: link the old inode to a second name before the new file replaces the first
  (the approach of `rename`-based atomic replacement that keeps a backup, as `vim`'s
  `backupcopy=no` and `rsync --backup` do).

## The decision

- Before each save, the file being replaced is hard-linked to
  `run/state.json.previous` (link to a temporary name, then replace), so the previous
  good version survives without copying it. Where a link cannot be made the save goes
  on without one.
- `update_state` reads through `_state_for_update`: the current file when it parses;
  else `state.json.previous` when that parses (logged as a recovery); else it raises
  `StateCorrupt` (an `OSError`, like `StateLockTimeout`, which every writer already
  treats as "not now") and writes nothing, so the corrupt file and its forensic copy
  stay as they are. Read-only callers keep `load_state` and its `{}`.

Files: `scripts/memory_state.py`,
`tests/test_a_corrupt_state_is_not_replaced_by_an_empty_one.py`,
`docs/research/2026-09-14-a-corrupt-state-is-not-replaced-by-an-empty-one.md`.
