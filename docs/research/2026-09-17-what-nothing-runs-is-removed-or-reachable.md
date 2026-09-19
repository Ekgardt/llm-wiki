# What nothing runs is removed, or made reachable

Dated 2026-09-17. Third audit, the dead-code rows of the retrieval area. The owner's rule for
this repository is "remove what is unneeded and useless, above all break nothing"; the
decisions were delegated. Each item below is either deleted with its tests and its contract
sentence, or wired in where a contract or a recorded signal needs a reader.

Files: `scripts/contextual_retrieval.py` (removed), `scripts/build_tiers.py`,
`scripts/retrieval_disposition.py`, `scripts/lookup_mode.py`, `scripts/search_memory.py`,
`benchmark/run_scale_matrix.py`, `benchmark/run_retrieval_v2.py`, `docs/ARCHITECTURE.md`,
`skills/knowledge-lookup/SKILL.md`, `tests/test_contextual_retrieval.py` (removed),
`tests/test_generation_integration.py`, `tests/test_context_compiler.py`,
`tests/test_scale_matrix.py`, `tests/test_build_tiers.py`

## What was found

- **`contextual_retrieval.py`, 848 lines.** No production module imports it: the generation
  builder (`evidence_graph_builder`), `doctor`, `search_memory` and `corpus_snapshot` never
  name it, and its `build_snapshot_contexts` is called only from
  `tests/test_generation_integration.py`. Its LLM half was already cut on 2026-09-11 as
  unreachable behind `_reject_contextual_llm`. The MCP tool called `get_context` is a
  different thing entirely — `mcp_server._get_context`, which reads pages.
- **The snapshot-tier island in `build_tiers.py`** — `build_snapshot_tiers`,
  `generate_l1_for_source`, `tier_artifact_key`, `_tier_entry`, `_tier_artifact_bytes`,
  `_staged_tier_artifact`, `_publish_tiers`, `_withdraw_tiers` and their helpers — writes a
  `tiers/tiers.json` artifact into a generation directory. Nothing in production builds or
  reads it either. The rest of the module is live: `scheduled_weekly` calls `build_all_tiers`
  and `build_advisory` reads `get_l1`, and `CLAUDE.md` documents
  `build_tiers.py --all`.
- **`docs/ARCHITECTURE.md:159`** lists "L0/L1/L2 tiers, contextual artifacts" among the
  derived state a generation seals. For contextual artifacts that was never true in the
  product; for the tier artifact it stopped being true.
- **`retrieval_disposition.refused_counts` / `refusal_gates`** have no caller at all, but they
  are the only readers of a signal the product deliberately writes: `query_memory` records
  `refused:<gate>` rows on every dropped claim. Deleting the reader of a recorded signal
  leaves the writing pointless.
- **LanceDB.** Retired from the product on 2026-09-07
  (`docs/research/2026-09-07-lancedb-was-never-reached.md`): no module, no dependency, no
  cache. Left behind: the `lancedb-flat` / `lancedb-ann` adapters of
  `benchmark/run_scale_matrix.py` (in its default adapter list), their two report schemas and
  the tests that pin them, plus prose in `docs/ARCHITECTURE.md`, `scripts/lookup_mode.py`,
  `scripts/search_memory.py` and `skills/knowledge-lookup/SKILL.md` that still describes it as
  the hybrid vector engine. A stand adapter for a store the product cannot have measures
  nothing the product can use.
- **`run_retrieval_v2.ModelEmbeddingAdapter._query_vectors` / `_query_sparse`** are read on
  every query and written nowhere, so the "cache" is a guaranteed miss.
- **`search_memory._embedded_matrix`** has no reference anywhere in `scripts/`, `tests/`,
  `benchmark/` or `integrations/`.

## Practice on this date

- The owner's standing rule, and the one that governs here: delete what nothing uses, but
  never delete something an outside caller may hold — a hook, a plugin entry point, a CLI
  named in the documentation, a scheduler target. Every deletion below was checked against
  `scripts/`, `tests/`, `benchmark/`, `integrations/`, `skills/`, `.github/`, the install
  scripts and the contract documents.
- The counterpart rule, which the same audit states: a feature a contract promises is wired
  in rather than deleted. A recorded signal is such a promise — the write is already paid for.

## The decision

- **Delete** `scripts/contextual_retrieval.py` with `tests/test_contextual_retrieval.py`, its
  two test call sites, and the words "contextual artifacts" in `docs/ARCHITECTURE.md`.
- **Delete** the snapshot-tier island of `build_tiers.py` and the tier half of the generation
  integration test; keep the page tiers, the CLI and `get_l1`. Remove "L0/L1/L2 tiers" from
  the generation sentence in `docs/ARCHITECTURE.md`, which now names what a generation really
  seals: the catalog, the evidence graph, FTS, vectors, telemetry and model caches.
- **Wire in** `refused_counts` and `refusal_gates`: `retrieval_disposition` gets a `main` that
  prints what carried answers and what was refused, so the signal the product writes has a
  reader an operator can run.
- **Delete** the two LanceDB adapters, their schema entries and the tests that pin them, and
  correct every remaining sentence that describes LanceDB as this product's vector engine.
  The hybrid tier is what the code does: numpy vectors over `intfloat/multilingual-e5-small`,
  plus the optional cross-encoder.
- **Delete** the two never-filled dictionaries in the stand's embedding adapter, so each query
  is encoded once per query as the product would, and the stand's timings stay honest.
- **Delete** `search_memory._embedded_matrix`.
- Not decided here, and named for the generation area: `search_memory.publish_generation`,
  `build_generation_numpy_vectors` and `_legacy_vector_source_membership` are referenced only
  by tests but belong to the generation builder, which another agent owns this round.
