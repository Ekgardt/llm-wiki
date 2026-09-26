# One part of a day per batch

Date: 2026-09-25. Audit item A-3 of `docs/AUDIT-2026-09-25-full.md`.

## Question

A daily log over the compile budget is split into parts. When two parts of one day
land in the same batch, only the first reaches the model, yet receipts are written
for both, so the second part is never compiled. How should batching treat parts?

## Sources

- "Fail-fast system", Wikipedia (fetched 2026-09-25,
  https://en.wikipedia.org/wiki/Fail-fast_system): a fail-fast system "immediately
  reports at its interface any condition that is likely to indicate a failure" and
  stops "rather than attempt to continue a possibly flawed process".
- `compile_memory.py`: `DailySnapshot.part_key`, `_group_dailies`,
  `_subset_compile_inputs`, `_deduplicated_sources` (read 2026-09-25).

## Findings (facts)

1. Sources are keyed by `logical_path` downstream (the prompt's source list, the
   evidence byte spans, which are relative to one part's bytes). Two parts of one day
   would present two different contents under one path, so `_deduplicated_sources`
   keeps the first and silently drops the rest.
2. `_group_dailies` packs parts by token budget only; nothing stops it from packing
   two parts of one day together. The batch manifest (`_source_descriptor` for every
   selected part) and the receipts cover every part, including the dropped one.
3. On the live vault 16 of 29 dailies have more than one part (about 4 MB).

## Decision (conclusion)

- The planner never puts two parts of one day in one batch: a part whose day is
  already in the current batch starts the next batch.
- `_deduplicated_sources` becomes a guard: a repeated path raises instead of being
  dropped, because it would mean a receipt for text the model never saw.
- Parts already covered by a receipt from a batch that dropped them keep that
  receipt; recompiling them is a separate backfill decision and is not automatic.

## Edited files

- `scripts/compile_memory.py`
- `tests/test_one_part_of_a_day_per_batch.py` (new)

## Uncertainty

How many already-receipted parts were never seen by the model is not measured here;
it needs the batch manifests of past compiles.
