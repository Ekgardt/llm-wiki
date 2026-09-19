# A rebuild takes the lease its siblings take

Dated 2026-09-18. Finding Q-L28 of the third audit (project journal).

Files: scripts/project_journal.py,
tests/test_a_journal_rebuild_holds_the_project_lease.py

## What was found

- `ProjectStore.rebuild_journal` reads every committed checkpoint, folds them into `journal.md`,
  `state.md` and any sealed segment, and writes all of it in one recoverable transaction — while
  holding no project lease. A checkpoint committed between `committed_events()` and the write is
  simply not in the fold: its row survives, so the entry is not lost for good, but the operator's
  rebuild reports a head that is already behind and `journal.md` is missing an entry until
  somebody rebuilds again.
- The two repairs of exactly this shape already take the lease. `repair_journal_gap.py` does
  `store.acquire_lease(project, "journal-gap-repair")` … `finally: store._release(lease)`, and
  `repair_orphaned_checkpoint_names.py` does the same under `checkpoint-name-repair`. The
  rebuild is the odd one out, which is what makes this a defect rather than a design choice:
  the class of operation already has a rule and one member does not follow it.
- The lease is also what a concurrent writer is watching. `_project_lease_precondition` puts the
  holder's token into every checkpoint transaction's preconditions, so a checkpoint that starts
  while the rebuild holds the lease is refused with `ProjectLeaseBusy` instead of racing it.

## Practice on this date

- This is the plain reader-writer rule for a derived file: a projection rebuilt from a log must
  exclude writers to that log while it reads, or it publishes a view that was never true. The
  lease already exists here and every neighbouring repair uses it; the research is which rule
  the codebase itself states, and it states this one twice.
- The owner's standing instruction for this round is the same shape: «Fix the class, not the
  instance» — a defect found in one member of a family is fixed for the family.

## The decision

- `rebuild_journal` takes the project lease for the whole read-fold-write, and releases it in a
  `finally`, exactly as its two siblings do. A rebuild started while another holds the project
  raises `ProjectLeaseBusy` rather than quietly rebuilding from a moving log.
- `_release_parked_checkpoints` stays inside the held lease: it hands the parked rows back to
  `recover`, and doing that under the same lease keeps the head it just published and the rows
  it releases consistent.
- The owner name is `journal-rebuild`, matching the operation id the transaction already uses.
