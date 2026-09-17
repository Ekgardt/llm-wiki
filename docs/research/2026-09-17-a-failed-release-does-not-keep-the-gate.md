# A failed release does not keep the gate

Dated 2026-09-17. Finding Q-H1 of the third audit (high, reproduced). The research before
the fix.

## What was found

- `MarkdownCoordinator._nested_writer_gate` releases in its `finally` by deleting the
  `writer_owners` row inside a transaction and then calling `_clear_gate()`. Nothing guards
  the pair: if the delete raises — realistically `database is locked` after the busy
  timeout — the thread-local `gate_depth` stays at 1 and the row stays in the table.
- From then on, in that process, a later `writer_gate()` enters as a re-entry without
  taking any gate and yields the stale owner; in every other process the row reads as a
  live writer and they get `owner_busy`. `_reclaim_dead_writer_projection` frees the row only
  when the lease has expired *and* the process is dead, so the gate stays shut until the
  affected process exits. The registry's release then fails on the foreign key as well.
- The sibling `_leave_canonical_gate` already has the `try/finally` this one lacks.
- Callers on the nested path: every project checkpoint (`project_journal.py:2687`), the
  compile (`compile_memory.py:2829`) and three sites in `markdown_transaction.py`.
- Code graph: `_nested_writer_gate` ← `writer_gate` ← those five callers.

## Practice on this date

- Release paths must restore local state unconditionally and leave durable state
  recoverable: SQLite documents that a busy database makes a write statement fail with
  `SQLITE_BUSY` after the busy handler gives up, and the application decides what to do
  next ([SQLite result codes, SQLITE_BUSY](https://www.sqlite.org/rescode.html#busy)).
  A lock whose release can fail needs a second way to be released; here the owner of the
  stale row is this very process, which can prove it and remove it on its next entry.

## The decision

- The release deletes the row in a `try` and always clears the thread's gate state in the
  `finally`, as the canonical gate does.
- A release that fails is recorded in the process (`_UNRELEASED_PROJECTIONS`: database,
  owner token, fencing epoch). On entry with that same lease the recorded row is removed
  before the dead-owner check. A row that merely looks like ours (same process, same thread)
  is not proof: a second coordinator in the same thread may hold it live, so without the
  record every row is judged by the registry exactly as before.

Files: `scripts/markdown_transaction.py`,
`tests/test_a_failed_release_does_not_keep_the_gate.py`,
`docs/research/2026-09-17-a-failed-release-does-not-keep-the-gate.md`.
