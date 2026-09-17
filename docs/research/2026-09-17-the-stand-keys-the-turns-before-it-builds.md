# The stand keys the turns before it builds

Dated 2026-09-17. Found by the third audit (retrieval, M2); the research before the fix.

Files: `benchmark/longmemeval_vault.py`, `tests/test_the_stand_keys_the_turns_before_it_builds.py`

## What was found

- Since 2026-09-16 the fact keys are indexed inside the generation: the build reads the key
  store at build time (`evidence_graph_builder._nightly_keys`) and, since `260295a`, writes
  them into the `keys` column of the search table.
- On the LongMemEval stand, `run_question` built the generation first and extracted the keys
  afterwards (`build_generation(...)`, then `_key_the_haystack(...)`). The store was empty
  when the build read it, so on the stand the `keys` column was always empty, whatever
  `LLMWIKI_BENCH_FACT_KEYS` said.
- The 2026-09-16 note promises that the separate keys leg is "measured against the new column
  in the next stand run, and the loser is removed". With this order that run would have
  compared the separate leg against nothing and removed the column for a fault of the stand.
- In the product the order is already right: the nightly pass keys the turns, and the next
  build indexes what it finds.

## Practice on this date

- Key expansion is an indexing-time step: the extracted facts are attached to the stored
  value before the index over it is built, and the query then matches them (Wu et al.,
  "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory",
  arXiv:2410.10813, the "fact-augmented key expansion" design). A stand that measures it has
  to follow the same order as the system it stands in for.

## The decision

- The stand's `build_generation` keys the turns itself, after it has collected the snapshot
  and before it builds, and reports the count as `build_info["keyed_turns"]` — the field the
  result record already had. `run_question` no longer keys after the build. An optional `ask`
  replaces the provider for the keying, so a test can script it. The keying is still off
  unless `LLMWIKI_BENCH_FACT_KEYS=1`; the other stands that call `build_generation` gain the
  same switch and are otherwise unchanged.
- No stand run was made for this fix (none was permitted). The test builds a generation on
  the stand's own path with a scripted provider and reads the `keys` column back.
- One consequence to know before the next run: the keying time now falls inside the stand's
  build timing, where before it fell between build and retrieval.
