# A code generation needs no vectors

Dated 2026-09-16. Twenty-three of the twenty-seven minutes of a full code-index build are
spent on vectors that no question reads. The research before removing them.

## What was measured and read

- The vault's code generation rebuilt on 2026-09-15 took 1612 s: the graph database and the
  lexical index were written by 12:50, the vector matrix at 13:13 — 23 minutes for 14 565
  chunks at 12-17 chunks a second (`intfloat/multilingual-e5-small`, 512-token cap, measured
  on this machine at two and four threads).
- Nothing reads those vectors. The dense leg has one entry,
  `retrieval._generation_vectors_search`, reached from `_dense_hits_or_none`, and the
  generation it opens comes from `_active_manifest_for` → `catalog.get_active_for_repository`
  — the **active** pointer. A code generation is registered and never activated
  (`GenerationCatalog.code_generation_for_repository`, decided 2026-09-12 in
  `docs/research/2026-09-12-the-vault-is-a-repository-too.md`). Code answers open the graph
  database only (`code_graph._opened_code_or_active`), and `get_architecture search` is SQL
  over graph nodes.
- The catalog already accepts a generation with `vector_state: absent`
  (`generation_catalog`, manifest default), so nothing about the format changes.
- The manifest of the vault's code generation also lists 333 sources under `knowledge/`,
  although its `code_roots` exclude that tree — a separate defect, recorded as a follow-up.

## Practice on this date

- Build what is read: dead artefacts cost the build, the disk and every cold open that
  hashes them. The same rule the product already applies to the reranker's model, which is
  loaded only when a chunk is not reused
  (`docs/research/2026-09-14-the-model-loads-only-for-new-chunks.md`).
- Keep the switch where the kind of generation is already known — the build policy — rather
  than adding a parameter down the call chain.

## The decision

- A generation whose policy names `code_roots` — a code generation — is built without
  vectors. Its manifest says `vector_state: absent`, which the catalog and the readers
  already understand.
- Memory generations are untouched: they are what the dense leg reads.
- If a code question ever needs a dense leg, the switch is one line and the generation is
  rebuilt; nothing in the format stands in the way.

Files: `scripts/evidence_graph_builder.py`,
`tests/test_a_code_generation_needs_no_vectors.py`,
`docs/research/2026-09-16-a-code-generation-needs-no-vectors.md`.
