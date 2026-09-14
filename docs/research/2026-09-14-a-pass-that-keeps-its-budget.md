# A pass that keeps its budget

Dated 2026-09-14. Item 3.3 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was observed

- Today `repository_index.py refresh-all --budget-seconds 900` was still running at
  1 000 s, when an outer timeout killed it; it had registered one generation at
  9 min and was writing a second. The nightly step is killed at budget + 120 s
  (`scheduled_nightly.STEP_START_MARGIN_SECONDS`), so a pass that overruns by more
  than two minutes loses its report and its lease release.
- The builder checks its deadline between stages (`_check_build_stop`), but
  `search_memory._built_generation_vectors` takes no deadline or cancellation:
  `_reused_matrix` hands every new chunk to one `embedder.encode` call
  (`_embedded_rows`). A first index of a repository or worktree embeds every chunk
  it has, so the pass cannot stop — for its deadline or for a lost fence — until
  the whole matrix is done.
- `repository_index._refresh_row` and `repository_worktrees._followed` catch only
  `RepositoryIndexRefused`. A `TimeoutError` from one build — the budget, or a
  cancelled fence, which the builder also raises as `TimeoutError` — escapes
  `refresh_all_repositories`: no JSON report, every repository after it skipped, the
  step recorded as failed. That is the traceback in
  `logs/maintenance/20260914T030102-repositories-445729.err.log`.
- The pass follows new worktrees first and refreshes registered repositories after,
  in a fixed order, so a worktree whose first build never fits the budget consumes
  it every night before any registered repository is looked at.

## Practice on this date

- Sentence Transformers encodes a large corpus by splitting it: "the input texts
  will be split into chunks of 1000 texts … and embedded in batches of 32 texts at a
  time"; `encode` itself has no cancellation, so a caller that must be able to stop
  encodes in chunks and decides between them
  ([large-scale encoding](https://sbert.net/examples/sentence_transformer/applications/computing-embeddings/README.html),
  [SentenceTransformer.encode](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)).
- This file's own stated rule for the step: "a repository that does not fit is
  deferred to the next night, never half-built" (`scheduled_nightly.py`, the
  comment on `REFRESH_ALL_BUDGET_SECONDS`). The builder already never publishes a
  half-built generation; what is missing is the deferral being reported instead of
  raised.
- Splitting re-batches texts, which moves float32 results by up to 8.6e-08; this
  vault has measured and accepted that for reuse
  (`docs/research/2026-08-28-what-a-rebuild-may-reuse.md`).

## The decision

1. **Vectors are embedded in chunks with a stop check between them.**
   `_embedded_rows` encodes `EMBED_STOP_BATCH = 256` texts at a time and calls the
   generation's stop check before each chunk; `build_generation_vectors_if_available`
   passes `_check_generation_stop(deadline, cancelled)`. A stop raises
   `TimeoutError`, which that function already re-raises, and nothing is published.
2. **A repository that does not fit is deferred and named, and the pass goes on.**
   `_refresh_row` and `_followed` turn a `TimeoutError` into
   `{"status": "deferred", "reason": …}` for that checkout. Once the shared deadline
   has passed, every remaining checkout defers at its first stop check, so the pass
   ends promptly with a complete report.
3. **Registered repositories are refreshed before new worktrees are followed.** An
   incremental refresh is cheap and keeps answers current; a first build of a new
   worktree is the expensive, optional part and now gets what budget is left.

Why not the alternatives:

- **Run the embedding in a thread and abandon it on timeout.** The thread keeps the
  CPU and the model's memory, and Python cannot stop it; the next step would start
  under its load.
- **Raise the step's kill margin.** The overrun is unbounded — it grows with the
  size of the first build.
- **Catch every exception per row.** A refusal is already named; other errors are
  defects and should still fail the step visibly.

Files: `scripts/search_memory.py`, `scripts/repository_index.py`,
`scripts/repository_worktrees.py`, `tests/test_a_pass_that_keeps_its_budget.py`,
`docs/research/2026-09-14-a-pass-that-keeps-its-budget.md`.
