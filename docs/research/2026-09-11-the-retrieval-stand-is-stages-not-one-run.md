# The retrieval stand is stages, not one run

Date: 2026-09-11. Trigger: audit finding H3 — `benchmark/run_retrieval_v2.py`
fails the complexity gate wholesale: 57 functions over CCN 5 in 5199 lines,
`_run_benchmark_once` at CCN 124 over 620 lines, `_aggregate_reports` 84,
`load_corpus` 71, `_orchestrate_selection_impl` 47, `_recompute_report_metrics`
44, `main` 41, ternary chains at 3399-3406 and 3497-3548. The file produces
the product's retrieval selection evidence; the audit's own words are that a
620-line function with nested fallbacks cannot be reasoned about for "did it
fall back silently".

## Sources

1. This repository, the same refactor done on five stands this week
   (`docs/research/2026-09-11-the-navigation-stand-is-named-steps.md`,
   `…-the-comparative-stand-checks-one-thing-per-function.md`,
   `…-the-scale-stand-is-one-adapter-cell-shape.md`): one object per run
   with a method per phase; one `_require_*` per validated section; tables
   instead of if/elif ladders; the error order and the report bytes are the
   contract and stay byte-identical.
2. The audit's constraint (H3, "Fix direction"): do not edit selection
   semantics. The frozen baseline is bound to exact report bytes
   (`knowledge/notes/baseline-environment-binding-decision.md`), so any
   change to metric arithmetic, ordering, tie-breaks or the canonical JSON
   would invalidate the recorded selection. The refactor therefore moves
   code and names steps; it never changes a number, a key, a message or the
   order in which errors are raised.
3. `tests/test_retrieval_v2_benchmark.py`: 122 tests pin messages, gate
   verdicts, report shape, worker payloads and orchestration order; they are
   the oracle for "unchanged".

## Decision

The file is refactored in four commits, each green on its test file and each
leaving its region at 0 lizard warnings at CCN 5, gate exit 0:

1. Loaders and lexical helpers (lines 272-1190): `_validate_matrix_candidate`
   as one `_require_*` per section, `load_model_selection` and `load_corpus`
   as pipelines of named checks, tokenizers, fusion, environment provenance,
   `_load_pinned_jieba` as named steps.
2. Adapters and vectors (1192-2060): `SQLiteLexicalAdapter` methods,
   `_search_vectors`, the model adapters' `encode`/`score`, `rank`
   implementations, `model_cache_environment`, cache-root checks.
3. The run (2061-3550): workspace, resources, gates, transformer loading,
   and `_run_benchmark_once` as a `_BenchmarkRun` object with a method per
   stage (corpus, lexical, embedding, reranker, fusion, evaluation, report)
   appending to the same report dict in the same order.
4. Selection and CLI (3553-5199): candidate policy, metric recomputation,
   baseline verification, `_aggregate_reports` and
   `_orchestrate_selection_impl` as named steps, `_validate_cli_args` as a
   list of checks, `main` as a dispatch table.

What is not done: no metric, threshold, key, message text, tie-break or
canonical byte changes; no change to worker argument order. Where a
fallback is silent today it stays silent, and a follow-up finding names it
rather than this refactor fixing it in passing.

Files: `benchmark/run_retrieval_v2.py`, `tests/test_retrieval_v2_benchmark.py`
(only if a private helper the tests import is renamed — none planned),
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
