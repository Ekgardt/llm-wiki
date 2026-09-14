# A prune inside its step

Dated 2026-09-14. Item 3.5 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

- `prune_generations.py` gives each removal its own deadline:
  `_discard_one` passes `deadline=time.monotonic() + PRUNE_BUDGET_SECONDS` (1 200 s)
  per generation, so a pass that removes *n* generations is bounded by
  1 200 s × *n*, not 1 200 s.
- The nightly runs it as Step 3d with a 300-second kill timeout
  (`scheduled_nightly.py`); the weekly with 1 200 s (`scheduled_weekly.py`). The
  parent kills the process group, possibly inside
  `GenerationCatalog.discard_superseded`, whose transaction rolls back while the
  files already unlinked stay unlinked. Normal passes take seconds (the nightly
  logs show removals of 100–337 MB within the same second), so this has not fired;
  a slow or stuck filesystem is exactly the case the budget was written for
  ("so a stuck filesystem ends the weekly step instead of the weekly pass").

## Practice on this date

- A child's own budget must end before its parent's kill, with room to report: the
  rule this codebase already states for the repository refresh —
  "The step's kill timeout sits above that budget by a margin for interpreter
  start-up and the deferral report, so the child's graceful deferral runs before the
  parent's kill" (`scheduled_nightly.py`, audit OPS-10). A deadline is a property of
  the pass, set once, not re-armed per unit
  ([Python `subprocess` timeouts kill the child](https://docs.python.org/3/library/subprocess.html#subprocess.run)).

## The decision

- `prune_generations(..., budget_seconds=PRUNE_BUDGET_SECONDS)` sets **one** deadline
  for the pass and hands it to every removal. A generation not started before the
  deadline is reported `DEFERRED:` and left for the next pass; a deferral is not a
  failure.
- `--budget-seconds` on the command line. The nightly step passes
  `300 − STEP_START_MARGIN_SECONDS` (180 s); the weekly passes `1 200 − 120` (1 080 s).

Why not the alternatives:

- **Raise the nightly timeout to 1 200 s.** The per-generation re-arming would still
  make the real bound unbounded in *n*.
- **Stop mid-removal at the deadline.** Already the behaviour inside one removal;
  starting a new one past the deadline is what this prevents.

Files: `scripts/prune_generations.py`, `scripts/scheduled_nightly.py`,
`scripts/scheduled_weekly.py`, `tests/test_a_prune_inside_its_step.py`,
`docs/research/2026-09-14-a-prune-inside-its-step.md`.
