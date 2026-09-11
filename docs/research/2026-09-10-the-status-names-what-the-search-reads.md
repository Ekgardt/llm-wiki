# The status names what the search reads

Date: 2026-09-10. Trigger: audit findings M2 and M3. `docs/USER-GUIDE.md`
still says "Plain `search_memory.py` always runs BM25. `--semantic` enables
vectors", while the flag is `BooleanOptionalAction` with default `True`
(`scripts/search_memory.py`), so the honest opt-out is `--no-semantic`. And
`search_memory.py --status` opens only the legacy `cache/index.sqlite`,
`--rebuild` rebuilds only that index, and the guide's remedy for "search
returns nothing" is that rebuild — while an answer on a vault with an active
generation comes from `cache/evidence-graph/…`, which neither command
touches.

## Sources

1. `scripts/search_memory.py`: `search()` → `retrieval.retrieve_via_search_memory`
   reads the active generation first (`_active_generation_catalog`,
   `GenerationCatalog.get_active`) and the legacy index only as fallback.
2. `scripts/generation_catalog.py` `_MANIFEST_KEYS`: the active manifest
   carries `generation_id`, `extractor_version`, `vector_state`
   (`absent|complete|stale`) and `embedding_model_id` — enough to say what
   the search will read.
3. The generation is built by the nightly (`scheduled_nightly`) and by
   `doctor.py --repair`; `search_memory.py` has no command that builds one,
   and adding one would duplicate the doctor's fenced, budgeted build.

## Decision

1. `--status` reports the active generation first (id, extractor, vector
   state, embedding model) or says there is none and that the search falls
   back to the legacy index; the legacy index lines follow as before.
2. `--rebuild` says, before rebuilding, that it rebuilds the legacy index
   only and names `doctor.py --repair` for the generation.
3. The guide documents the default and `--no-semantic`, and the remedy for
   an empty search is `doctor.py` (state) then `doctor.py --repair`.

Files: `scripts/search_memory.py`, `tests/test_search_ranking.py`,
`docs/USER-GUIDE.md`, `CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
