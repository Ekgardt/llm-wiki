# The embedding model loads only when a chunk needs it

Dated 2026-09-14. The model-load part of item 5.1 of `docs/AUDIT-2026-09-14-2.md`. The
research before the change.

## What was found

- `search_memory.build_generation_vectors_if_available` calls `_get_embedder()` — the
  `SentenceTransformer` load — before `_built_generation_vectors` looks at the parent's
  cached rows (`_reusable_vector_rows`, `_reused_matrix`). The audit measured the load at
  6.8 s and 1.2 GB resident on this machine. On a night where no page changed, every
  chunk is reused and the model is never asked for a vector, but it is loaded anyway, in
  every process that builds a generation.
- `_reused_matrix` calls the embedder only through `_embedded_rows`, which returns an
  empty block without calling it when no chunk is fresh.
- An unavailable model (package missing, weights absent, load error) is recorded by
  `_get_embedder` and turned into `None` → `vector_state: absent`
  (`tests/test_embedder_unavailable_names_its_reason.py`).
- The code graph: `build_generation_vectors_if_available` ←
  `evidence_graph_builder` (the generation build, nightly and on demand);
  `_get_embedder` ← that path, the query encoder and the MCP warm-up.
- Not addressed here, from the same audit item: the 11.5 MB incremental manifest per
  generation and the repeated corpus scans at night.

## Practice on this date

- Lazy initialisation of an expensive dependency at its first use, with the failure
  reported at that point, is the pattern this module already uses for queries
  (`_lazy_generation_query_encoder`, whose docstring records the measured cost of
  loading eagerly).

## The decision

- The build first checks that `sentence-transformers` is importable
  (`_have_sentence_transformers`, no model load). It hands `_built_generation_vectors` a
  lazy passage encoder that loads the model on its first call; if the load fails there,
  the build returns `None` as before (`vector_state: absent`), with the reason recorded
  by `_get_embedder`.
- When every chunk is reused, the model is not loaded and the generation's vectors are
  the parent's rows for the same model and revision — the rows `_reusable_vector_rows`
  already verified.

Files: `scripts/search_memory.py`, `tests/test_the_model_loads_only_for_new_chunks.py`,
`docs/research/2026-09-14-the-model-loads-only-for-new-chunks.md`.
