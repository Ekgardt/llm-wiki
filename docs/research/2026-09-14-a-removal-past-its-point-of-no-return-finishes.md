# A removal past its point of no return finishes

Dated 2026-09-14. Item 2.7 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `GenerationCatalog.discard_superseded` deletes a superseded generation's rows
  (`generations`, `activation_history`) and its tree inside one write transaction:
  rows first, then `_remove_generation_tree` (`shutil.rmtree`), then
  `_check_cancelled` and `_check_deadline`, then the commit.
- If the tree is gone and the transaction does not commit — the deadline check after
  `rmtree` raises, a cancel arrives, or the process is killed in that window (the
  nightly prune step is killed at 300 s) — the rows come back and the tree does not.
- From then on the generation is "registered but no tree": `plan_prune` puts it in
  `unpaired`, `prune_generations._report` exits 1 for every unpaired generation, and
  `discard_superseded` refuses it ("generation tree is missing",
  `_require_paired_generation`). The nightly and weekly passes fail every run, and
  nothing ever clears it. Reproduced by the audit.
- A partial `rmtree` failure is not this case: the directory still exists, the rows
  roll back, and the next prune retries (`test_a_failed_tree_removal_rolls_the_registration_back`).
- The code graph: `discard_superseded` ← `prune_generations._discard_one` ←
  `_discard_reporting_failure` ← `_applied`; `plan_prune` ← `prune_generations`.
  A test pins today's refusal: `test_a_registration_without_a_tree_is_refused_not_deleted`.

## Practice on this date

- A multi-step operation with an irreversible step recovers *forward* once that step
  has happened: sagas distinguish backward recovery (compensate) from forward recovery
  (retry to completion), and a step that cannot be compensated must be followed only by
  steps that are retried until they succeed
  ([Garcia-Molina & Salem, "Sagas", SIGMOD 1987](https://dl.acm.org/doi/10.1145/38713.38742);
  [microservices.io, Saga](https://microservices.io/patterns/data/saga.html)).
- A cancellation that arrives after the point of no return is honoured after the
  operation completes, not by abandoning it half-done (the same rule `threading` and
  asyncio shielding follow for cleanup that must finish).

## The decision

- `discard_superseded` does not check the deadline or cancellation after the tree is
  removed; the commit follows the removal directly.
- Forward recovery for what an interrupted discard left: a registration whose tree is
  missing, that was activated, and that retention does not keep, is a discard that
  did not commit. `plan_prune` lists it as prunable, and `discard_superseded` completes
  it by deleting the rows. A retained or never-activated registration without a tree is
  still reported `UNPAIRED` and refused, as before — nothing proves what happened to it.

Files: `scripts/generation_catalog.py`, `scripts/prune_generations.py`,
`tests/test_prune_generations.py`,
`tests/test_a_removal_past_its_point_of_no_return_finishes.py`,
`docs/research/2026-09-14-a-removal-past-its-point-of-no-return-finishes.md`.
