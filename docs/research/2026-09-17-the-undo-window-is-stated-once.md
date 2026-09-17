# The undo window is stated once

Dated 2026-09-17. Finding M12 of the third audit (Markdown transactions). The research before
the fix.

Files: scripts/markdown_transaction.py, tests/test_the_undo_window_is_stated_once.py

## What was found

- The owner changed the undo window from thirty days to two on 2026-09-02
  (`docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`: "Nobody keeps undo
  data for thirty days for crash safety. It exists to finish or unwind a write in flight, and
  then it is garbage."). `UNDO_RETENTION_DAYS = 2` drives `prune`, the doctor and the
  installed-vault repair.
- Two literals kept the old figure. `_within_undo_retention` (behind
  `MarkdownCoordinator.deletion_blockers`) reports a committed, unpruned transaction as an
  `undo_retention` blocker for thirty days, while the doctor stops at two: the coordinator
  and the doctor disagree about whether `run/` may be deleted. `_require_undoable` refuses
  only after thirty days, and its message names a "30-day undo window" that no longer exists.
- `CLAUDE.md`, `AGENTS.md` and `docs/STRUCTURE.md` still promise a thirty-day undo window.
- Code graph: `_within_undo_retention` ← `_transaction_deletion_blocker` ←
  `deletion_blockers`; `_require_undoable` ← `undo` ← the CLI and the MCP doctor action.

## Practice on this date

- The decision record quoted above is the source: the window is two days, by the owner's
  decision, with its own research and sources (PostgreSQL point-in-time recovery, Oracle undo
  retention). A figure that a decision changed has to change everywhere the code states it,
  and the way to make that hold is to state it once.

## The decision

- Both literals become `UNDO_RETENTION_DAYS`, and the refusal names the window from the same
  constant. An undo asked after the window answers `undo_window_expired` whether or not
  `prune` has already run, so the answer no longer depends on the timing of maintenance.
- The three contract documents are not edited here: they are the owner's operating contract.
  They are listed in the report as still saying thirty days.
