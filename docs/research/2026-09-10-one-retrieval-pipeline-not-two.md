# One retrieval pipeline, not two

Date: 2026-09-10. Trigger: audit finding H1 (`docs/AUDIT-2026-09-10-memory-and-retrieval.md`):
`search_memory._search_backends` has no caller anywhere, and it is the only
root of a second pipeline — legacy BM25/vector/graph fusion, its own
reranker call, its own generation search — that the live product never
enters. The live path is `search()` → `retrieval.retrieve_via_search_memory`
→ `retrieval.fuse_rrf`. Tests referenced the dead path only to assert it is
not called, while `tests/test_search_ranking.py` still unit-tested the dead
`_rrf_fuse_triple` and `tests/test_reranker.py` the dead `_maybe_rerank`;
`docs/STRUCTURE.md` still called the module "triple-RRF".

## Sources

1. Fowler, *Refactoring* catalog, "Remove Dead Code": unused code is
   removed rather than kept "in case", because the version control system
   holds it; the motivation is that a reader spends effort understanding
   code that has no effect. https://refactoring.com/catalog/removeDeadCode.html
2. The owner's standing instruction (2026-09-10): «Всё что ненужно и
   бесполезно удаляй, главное ничего не сломай».
3. Evidence in this checkout: an AST pass over `scripts/search_memory.py`
   computed the set of module-level functions reachable only from
   `_search_backends` and referenced by no other file under `scripts/`,
   `benchmark/`, `integrations/` and by no module-level statement — 32
   functions, plus `_rrf_fuse_triple`, `_maybe_rerank`, `_reranked` and
   `_deduplicate_by_slug`, whose only production callers are in that set
   (the AST pass had kept them because a docstring in `graph_neighbors.py`
   and a same-named function in `reranker.py` matched the name). The
   codebase graph agrees: `_search_backends` callers_total 0.

## Decision

Delete the subtree and the tests that exist only for it; remove the
monkeypatch guards that asserted the dead path is not entered (the
assertion is now structural); update `docs/STRUCTURE.md`, `tests/README.md`
and the `graph_neighbors.py` docstring to name the pipeline that runs. No
retrieval behaviour changes: nothing on the live path is touched, and the
suite proves it.

Files: `scripts/search_memory.py`, `scripts/graph_neighbors.py`,
`tests/test_search_ranking.py`, `tests/test_reranker.py`,
`tests/test_retrieval_review_blockers.py`, `tests/README.md`,
`docs/STRUCTURE.md`, `CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
