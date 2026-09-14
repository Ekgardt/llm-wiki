# An abandoned publication is collected, and a stray tree does not fail the night

Dated 2026-09-14. Item 5.5 of `docs/AUDIT-2026-09-14-2.md`. The research before the change.

## What was found

- `prune_generations.plan_prune` puts every registered generation that was never
  activated in `pending` and leaves it forever: `_require_activated` explains that "a
  build in flight looks exactly like an abort", and `discard_unactivated` — the path for
  an abort — runs only inside the build that registered it
  (`evidence_graph_builder._discard_failed_generation`). A build killed between
  `register` and `activate` never reaches it.
- Read-only on the live vault today (`prune_generations.py` without `--apply`, and the
  catalog opened read-only): four pending generations, e.g.
  `generation-18d4ae408d89b954-40403af9` 289 MB and `generation-18d52b65a4cc1690-b3500476`
  271 MB; the audit counted 956 MB. None of them is being built.
- The same run shows one `UNPAIRED` generation: a tree with no registration, last written
  at 11:19 today, 248 MB — an aborted build's directory. `_report` counts every unpaired
  generation as a failure, so the nightly prune step exits 1 until something removes that
  tree; the doctor's repair removes such trees, but only once they are a day old
  (`docs/research/2026-09-14-a-build-in-flight-is-not-an-orphan.md`). A registration
  without a tree is a different case and stays a failure (except a superseded one, which
  is completed — `docs/research/2026-09-14-a-removal-past-its-point-of-no-return-finishes.md`).
- A build writes into its generation directory as it goes and finishes, registration to
  activation, within one builder budget of at most 15 minutes.
- The code graph: `plan_prune` ← `prune_generations`; `discard_unactivated` ←
  `evidence_graph_builder` build paths, `repository_retention._discarded`;
  `doctor._written_within_grace` ← `_skip_generation_child`.

## Practice on this date

- The same grace-period rule as the orphan repair: what no writer has touched for far
  longer than any writer runs is not in flight (git `gc.pruneExpire`, cited there).

## The decision

- One definition of "not touched for a day": `generation_catalog.untouched_for(path,
  seconds)` and `ABANDONED_AFTER_SECONDS = 24 h`, used by the doctor's orphan repair and by
  the prune.
- The prune plan separates `abandoned` (registered, never activated, tree untouched for a
  day) from `pending`. Applied, it removes each abandoned one through
  `discard_unactivated` — the catalog's own path for an aborted publication — within the
  pass's shared budget. A fresh pending generation is left as before.
- A tree with no registration is reported `ORPHAN` and is not a failure of the prune: the
  doctor's repair owns it. A registration without a tree that is not a completed discard
  is still `UNPAIRED` and still fails the pass.

Files: `scripts/generation_catalog.py`, `scripts/prune_generations.py`, `scripts/doctor.py`,
`tests/test_prune_generations.py` (its unpaired case becomes a registration without a
tree; its plan case lists the stray tree as an orphan), `tests/test_an_abandoned_publication_is_collected.py`,
`docs/research/2026-09-14-an-abandoned-publication-is-collected.md`.
