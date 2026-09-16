# The code index leaves the knowledge alone

Dated 2026-09-16. The vault's code generation carries 333 copies of the vault's private
pages. The research before the fix.

## What was found

- The source manifests on this machine: the active memory generation
  (`generation-18d560382cd1fa3b`) holds 333 sources, all under `knowledge/`, with empty
  `code_roots`. The code generation built yesterday (`generation-18d57f630328e187`) holds
  1 524 sources — the code roots **and the same 333 knowledge pages**.
- `corpus_snapshot._discover` always calls `_walk_knowledge`, which walks
  `knowledge/notes` and `knowledge/projects`, whatever the policy says. Only the code-root
  list is policy-driven, and `repository_index.selected_code_roots(..., memory_owner=True)`
  deliberately strips `knowledge` from it — so the intent that a code generation carries no
  memory is already written down in the code that chooses the roots, and undone by the
  collector.
- The decision this contradicts: `docs/research/2026-09-12-the-vault-is-a-repository-too.md`
  — "the vault carries two generations of one checkout": memory, which the active pointer
  names, and code.
- The cost measured yesterday: those pages are 28 % of the code generation's chunk tokens;
  they were embedded twice (now once, since a code generation builds no vectors), and they
  still cost the graph build, the lexical index and the disk.

## Practice on this date

- One corpus, one policy: what a build collects should be decided by the policy it was
  given, not by a hard-coded walk. Anything else makes two callers disagree about what a
  generation is, which is what happened here.

## The decision

- `_discover` walks the knowledge tree only when the policy names no code roots — that is,
  for a memory generation. A code generation collects exactly its code roots.
- Nothing changes for the memory generation, whose `code_roots` are empty, nor for a foreign
  repository, which has no `knowledge/` tree to walk.

Files: `scripts/corpus_snapshot.py`,
`tests/test_the_code_index_leaves_the_knowledge_alone.py`,
`docs/research/2026-09-16-the-code-index-leaves-the-knowledge-alone.md`.
