# An answer is stale when any source its index holds has moved

Date: 2026-09-25. Audit item B-19 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- The corpus generation indexes `knowledge/notes/**/*.md` and, per project, only
  `state.md` and `context.md` (`corpus_snapshot._walk_knowledge`,
  `PROJECT_FILES`); `journal.md` is deliberately not indexed.
- `mcp_server._index_is_behind` calls a result stale when the newest note's
  mtime is later than the generation's manifest. It never looks at the project
  files the generation also holds, and a deleted or renamed note moves no file's
  mtime, so an answer drawn from a page that is gone still says `fresh`.
- The audit also named `journal.md`; a journal change cannot make an answer
  stale because the journal is not in the index. That part of the finding does
  not hold.

## Source

- Linux `inode(7)`, https://man7.org/linux/man-pages/man7/inode.7.html (fetched
  2026-09-25): "the mtime timestamp of a directory is changed by the creation or
  deletion of files in that directory." The page does not say whether a rename
  within one directory changes it; a rename is a creation of the new name and a
  deletion of the old, and on this machine's ext4 it does (checked in the test).

## Decision

- The newest source time is the latest of: every note's mtime, every directory's
  mtime under `knowledge/notes/` (a removal or rename updates it), and every
  project's `state.md` and `context.md` mtime.

## Files

- `scripts/mcp_server.py`
- `tests/test_an_answer_is_stale_when_any_indexed_source_moved.py`
- `CHANGELOG.md`
