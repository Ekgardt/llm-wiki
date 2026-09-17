# A question is answered by its own kind of generation

Dated 2026-09-17. Third audit, second round: findings G-M3, G-M5, G-M9, G-L10. The research
before the fix. The decisions were delegated by the owner ("decide after the five rules").

Files: `scripts/generation_catalog.py`, `scripts/evidence_graph.py`,
`scripts/prune_generations.py`, `scripts/repository_retention.py`,
`scripts/repository_index.py`, `scripts/corpus_snapshot.py`,
`scripts/evidence_graph_builder.py`, `scripts/doctor.py` (the extraction adapter only),
`scripts/code_graph.py` (`_opened_code_or_active` only),
`tests/test_a_question_is_answered_by_its_own_kind_of_generation.py`,
`tests/test_a_code_root_named_knowledge_is_code.py`.

## What was found

- Since 2026-09-12 one checkout carries two kinds of generation. A memory generation is
  built with no code roots and is the only kind the active pointer names. A code generation
  is built from code roots, is registered and never activated, and since 2026-09-16
  (`_walk_memory`) holds no knowledge page at all. The two kinds are disjoint by construction.
- G-M3 A. `GenerationCatalog._scoped_generation` — what `get_active_for_repository` answers
  with when the pointer is empty or names another repository — returns the newest registered
  generation of the scope whatever its kind. The nightly order is code refresh first, memory
  build second, so on the first night of an install, and on any night the memory build is
  deferred, a memory question (`search_memory._active_for_repository`,
  `retrieval._active_manifest_for`, `graph_neighbors`) is handed the code generation:
  answers come from `scripts/` and `tests/` chunks, with no vectors, and the legacy index
  that would have answered is never consulted. Reproduced: empty pointer, one registered
  code generation, `get_active_for_repository(vault scope)` returns it.
- G-M3 B. `EvidenceGraph.open_code_for_repository` returns `None` both for "this repository
  has no code generation" and for "it has one and it cannot be opened".
  `code_graph._opened_code_or_active` then opens the active pointer; on the vault that is
  the memory generation, which holds no code. A damaged code generation becomes a confident
  empty code answer.
- G-L10. `code_generation_for_repository` returns the newest matching registration without
  validating it, while retention deliberately keeps a second code generation. Nothing ever
  reads the second one: when the newest is damaged the answer is "none".
- G-M9. "Holds code" is read from the manifest field `code_roots`, added on 2026-09-12. A
  generation of another repository built between 2026-08-28 and 2026-09-12 has no such
  field, so it reads as a memory publication: refresh says `not_indexed` for ever, and the
  pruner removes it as an abandoned publication after a day. The snapshot policy inside
  `source-manifest.json` — whose digest the manifest binds — has carried the roots all along;
  `repository_index._covered` already reads them from there for the listing.
- G-M5. Extraction is routed by the path prefix `knowledge/`
  (`doctor._SourceExtractionAdapter._result_for`, `_code_extraction_sources`,
  `_knowledge_extraction_sources`, `evidence_graph_builder._workspace_source_ids`). Another
  repository's tracked top-level `knowledge/` directory is one of its code roots, so
  `knowledge/mod.py` there goes through the knowledge extractor: no functions, no calls.
  Reproduced by the audit (`knowledge-page: 1`, one symbol instead of three).

## Practice on this date

- PEP 20: "Errors should never pass silently. Unless explicitly silenced." and "In the face
  of ambiguity, refuse the temptation to guess." (https://peps.python.org/pep-0020/). Both
  halves of G-M3 are a guess made silently: the newest generation of the scope is assumed to
  be of the kind the caller wants.
- The repository's own rule, `docs/research/2026-09-12-the-vault-is-a-repository-too.md`:
  the pointer belongs to memory, a code reader asks for the code generation by kind. The
  defects are the places that rule was not applied to the fall-through paths.
- `corpus_snapshot.py` already states the principle for directory pruning: the vault's
  nouns are "this vault's nouns, not facts about repositories".

## The decisions

1. **One predicate for the kind.** `GenerationCatalog.holds_code(identifier, manifest)`:
   the manifest's `code_roots` when the field is present; otherwise the `code_roots` of the
   snapshot policy in the generation's `source-manifest.json`, read only after its bytes
   match the digest the manifest records, bounded by `MAX_SOURCE_MANIFEST_BYTES`, and
   remembered per generation id for the life of the catalog object (a generation is
   immutable). An unreadable source manifest answers "holds no code" — such a generation
   cannot be read by anyone. `code_generation_for_repository`, `prune_generations` and
   `repository_retention` all ask this one predicate (G-M9). No format change, no migration.
2. **The pointer path answers memory questions only.** `_scoped_generation` skips a
   generation that holds code. With an empty pointer and only a code generation registered,
   `get_active_for_repository` answers `None` and memory retrieval falls back to the legacy
   index, which is what the contract says it is kept for (G-M3 A). Another repository has no
   memory generation, and its code readers already use `open_code_for_repository`.
3. **A code reader walks the kept generations.** `code_generations_for_repository` yields
   every code generation of the scope, newest first; `open_code_for_repository` opens the
   first that validates (G-L10). `code_generation_for_repository` stays "the newest
   registered" for the refresh and the parent of a build, which must not quietly build on a
   generation other than the one they name.
4. **A repository whose code generation cannot be opened gets no memory answer.**
   `_opened_code_or_active` falls through to the active pointer only when the repository has
   no code generation registered at all (a vault indexed before 2026-09-12, test fixtures).
   When one is registered and none opens, the answer is `None` — the callers' existing
   "no usable index" path — never the memory generation (G-M3 B). `code_graph.py` belongs to
   another area; this is the smallest change there.
5. **A path is memory only when no code root holds it.** `corpus_snapshot.is_memory_path(
   relative_path, code_roots)`: under `knowledge/` and not under any of the snapshot's code
   roots. The vault's code roots never include `knowledge` (`selected_code_roots` removes it
   and refuses it when requested), so nothing changes for the vault; a repository whose
   `knowledge/` is a code root has it extracted as code (G-M5). The chunker's
   `_source_kind` is left alone: it reconstructs chunks from a path alone and is pinned by
   the reproducibility validators; it decides the chunk's source type, not the extractor.

## What this costs

On the vault a code question reads one `source-manifest.json` per memory generation that is
newer than the newest code generation — after a night, one file, once per catalog object.
