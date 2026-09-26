# A killed prune neither blocks undo nor outruns its step

Date: 2026-09-26. Audit 2026-09-26, finding C-12 (`prune()` holds the gate without a
deadline; undo is refused during an interrupted prune).

## What was wrong (facts, read in the code)

- `reclaim_runtime_state.prune_settled_transactions` called `coordinator.prune()` with
  no deadline, although `prune` takes one. The nightly kills the reclaim step after 180
  s (`maintenance_helpers.run_step`), and the backlog drain before the prune may use 120
  s of them. A kill inside a prune leaves `.<id>.pruning-<uuid>` behind.
- While such a staged directory existed, `undo` of that transaction was refused with
  "transaction undo images are no longer retained": undo checked the image directory
  and never looked for the staged copy. `prune` and `recover` restore it; `undo` did not.

## Source

Python documentation, `subprocess.run`, fetched 2026-09-26 from
https://docs.python.org/3/library/subprocess.html: "If the timeout expires, the child
process will be killed and waited for." A killed child gets no chance to finish or
clean up; only work that stops itself before the kill ends cleanly.

## Decision

- `RECLAIM_STEP_SECONDS` (180) lives in `reclaim_runtime_state` and the nightly takes
  the step's kill time from it. The reclaim pass gives the image prune a deadline 30 s
  (`RECLAIM_MARGIN_SECONDS`) before that; a prune that reaches it stops between
  transactions (its existing per-row check), reports `unfinished`, and the next night
  continues. It is not a failure.
- `_require_retained_undo_images` restores interrupted prunes (under the gate undo
  already holds) before it concludes the images are gone. A row that the prune already
  marked keeps its refusal, because `_restore_staged_prune` puts images back only for a
  row that still owns them.

## Files

- `scripts/markdown_transaction.py`
- `scripts/reclaim_runtime_state.py`
- `scripts/scheduled_nightly.py`
- `tests/test_a_killed_prune_neither_blocks_undo_nor_outruns_its_step.py`
