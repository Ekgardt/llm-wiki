# Nothing half-written is left behind

Dated 2026-09-18. Findings Q-L10 and Q-L13 of the third audit (Markdown transactions).

Files: scripts/markdown_transaction.py,
tests/test_nothing_half_written_is_left_behind.py

## What was found

Two places in the coordinator create a file or a directory under a temporary name and remove it
only on the path where everything went right.

**Q-L10, the prune.** `_prune_one` renames a transaction's image directory to
`.<id>.pruning-<uuid>` before marking the row pruned, so a failure can put it back. It does put
it back for an exception — but not for a process that dies, and this one is scheduled: the
nightly prune runs while the machine may be shut down, and `LLM_WIKI_TRANSACTION_KILLPOINT`
exists precisely because this code is expected to be killed mid-flight. What is left is a
directory under `run/` that nothing ever looks at again: `_prunable_rows` selects by
`artifacts_pruned_at IS NULL`, and `_prune_one` then finds no `artifact_root` and returns 0. The
images are alive on disk, unreachable, and counted by nothing. Every interrupted prune adds one.

**Q-L13, the Windows publish.** `_publish_at_parent`, the POSIX path, removes its
`.<name>.<uuid>.tmp` in a `finally`. `_publish_windows_target`, the same operation through
Windows handles, removes it only when `durable_publish_file` answers `"duplicate"`. A deadline
that lands in `_before_target_mutation`, a publication the DLP guard blocks, a sharing violation
inside `durable_publish_file` — each leaves the temporary beside the page it was meant to
become. That directory is `knowledge/`, not `run/`: the debris lands in the vault the user reads
and Git sees, and it is dotted so it is easy not to notice.

## Practice on this date

This is the oldest rule in file-based durability, and SQLite's own rollback journal is the
worked example. On the happy path, "After the database changes are all safely on the mass storage
device, the rollback journal file is deleted." On the unhappy path that deletion never happens,
and SQLite does not rely on it having happened: the next connection to open the database looks
for the leftover — a *hot journal*, which the same document defines by a list of conditions the
opener tests — and rolls it back before doing anything else
([SQLite, *Atomic Commit In SQLite*](https://www.sqlite.org/atomiccommit.html)). The point is not
that the cleanup runs in a `finally`; it is that *something* looks for the leftovers afterwards,
because a `finally` cannot run in a process that is gone.

This coordinator already applies that rule everywhere else — `recover()` walks unfinished
transaction rows, adoption re-reads its candidates, the writer gate reclaims a dead projection.
The two paths above are the ones with no sweeper behind them.

## The decision

- `prune` begins, under the writer gate it already holds, by putting back every
  `.<id>.pruning-*` directory it finds. The gate makes that safe to do without a second
  ownership notion: no other prune can be mid-rename while this one holds the gate, so a staged
  directory under it is the debris of a process that died. If the transaction's own image
  directory is somehow present as well, the staged copy is removed instead — the live one wins.
  Restoring rather than deleting is deliberate: the row still says the images are retained, and
  after the restore the ordinary prune redoes the work and gets it right.
- `_publish_windows_target` removes its temporary in a `finally`, as its POSIX twin does. A
  successful publish has already moved the file, so the removal is a no-op there, and an `OSError`
  from the removal itself never replaces the error that is on its way up.
