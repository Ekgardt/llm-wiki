# An answer says how old its index is

Dated 2026-09-24. Audit items B-4, B-6 (the pending count), C-2, C-10 and C-11
(`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/compile_memory.py`, `scripts/mcp_server.py`, `scripts/mcp_contract.py`,
`scripts/lookup_mode.py`, `scripts/search_memory.py`, `scripts/doctor.py`,
`scripts/prune_generations.py`, `tests/test_an_answer_says_how_old_its_index_is.py` (new),
`tests/test_mcp_server.py`, `CHANGELOG.md`,
`docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.

## What was found (live vault, 2026-09-24)

- **B-4.** The memory generation is refreshed only by the nightly. A page compiled at 11:20
  was not found by search for the rest of the day, and `recall` said `fresh` with no warning:
  `_signal_freshness` returns `fresh` for every signal that was used, whatever the index's
  age. The envelope's `index_timestamp` is hardcoded `None` (`mcp_contract.py`), a field that
  was declared and never filled.
- **C-2.** Doctor calls the generation stale whenever `unindexed_delta > 0` or it is older
  than 24 hours. Every capture appends to a daily log, so the vault is `degraded` for most of
  every day although nothing is wrong; and an unchanged vault turns `degraded` after 24 hours
  because an up-to-date refresh (`status: current`) builds nothing and the age is read from
  the manifest's mtime.
- **C-10.** `lookup_mode.py` recommends `BASE` by page count (a rule from before the
  generation existed), `wiki_overview` reports that as `retrieval_tier`, and both CLI and MCP
  search in `HYBRID` regardless (`retrieval._requested_profile`). `recall` reports a `graph`
  component as `missing` on every answer although `HYBRID` never asks for graph
  (`PROFILE_SIGNALS["HYBRID"] == ("lexical", "dense")`). `search_memory --status` says
  "Pages on disk: 195" and `lookup_mode` 199: the four between are superseded pages, which
  retrieval excludes by rule 12 — two true numbers under misleading labels.
- **B-6 (count).** `prune_generations` reports every never-activated registration as
  "pending activation", including code generations, which its own docstring says are never
  activated by design: the live "6 pending" were all code generations, and the number could
  hide a real stuck memory publication.
- **C-11.** A CLI search takes about 8.5 s; profiled, 2.7 s is loading the embedder
  (`sentence_transformers` → `torch`, `transformers`, `sklearn`, `scipy` imports) and most of
  the rest the same imports' side effects. Result lines print only the chunk's heading, and
  `transformers` prints a "Loading weights" progress bar on every run.

## Practice on this date

- A cache that answers must say how old it is, and a projection must be refreshed when its
  source changes rather than on a timer alone: the generation build is incremental and
  returns `status: current` without building when nothing changed, so refreshing after the
  write that changed the source costs little. (Fowler, "Event Sourcing": state "can be
  rebuilt"; here it is kept close to the log by rebuilding after each append of pages.)
- Status output reports what the system does, not a recommendation nothing follows.

## The decisions

1. After a compile that ran from the command line (the spawned background compile) finishes
   successfully, it runs one bounded `run_generation_maintenance`. It is a no-op
   (`status: current`) when no source changed, and defers if the maintenance fence is held
   (the nightly refreshes after its compile anyway). The in-process MCP `compile` is bounded
   by the tool's deadline and does not refresh.
2. `recall` compares the active generation's manifest time with the newest page under
   `knowledge/notes/`; when pages changed after it, the `lexical` and `dense` components are
   `stale` (the envelope then says so) and `index_timestamp` carries the manifest time for
   `recall` and `get_context`. The check is a directory `stat` walk of the notes, bounded by
   the page count.
3. Components report only signals the requested mode declares (`PROFILE_SIGNALS`), so graph
   is not "missing" from an answer that never asked for it.
4. Doctor: the generation is stale when its identity is stale, or when sources changed and it
   has not been refreshed for a day (`delta > 0 and age > 24 h`); an unchanged vault is not
   stale by age.
5. `lookup_mode` reports the mode search uses — `HYBRID` with a generation whose vectors are
   complete, `BASE` with a generation without them, direct Markdown with none — and the page
   counts under names that say what they count (searchable, superseded). `wiki_overview`'s
   `retrieval_tier` carries the same. The size tiers are removed.
6. `prune_generations` counts as pending only never-activated **memory** publications.
7. CLI: each result prints the first lines of its text after the heading, and the
   `transformers` progress bar is disabled. The embedder's import cost is a limit of the
   `sentence-transformers`/`torch` stack; replacing it is a separate change with its own
   measurement (an ONNX encoder must reproduce the stored vectors), not done here.

## Limits (rule 3)

A page written by hand outside the compile is found after the next compile or nightly, and
`recall` says so meanwhile. The mtime comparison can report `stale` after a touch that did
not change content; it never reports `fresh` for a changed page.

## Sources

- Martin Fowler, "Event Sourcing" — https://martinfowler.com/eaaDev/EventSourcing.html — fetched 2026-09-24.
- `cProfile` of `search_memory.py "nightly steps"` and `doctor.py --json` on the live vault, 2026-09-24.
