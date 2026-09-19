# Small corrections in the generations area

Dated 2026-09-17. Third audit, the low findings of the generations report: G-L1, G-L2,
G-L3, G-L4, G-L5, G-L8, G-L9, G-L11. The research before the fixes.

Files: `scripts/repository_index.py`, `scripts/code_hints.py`, `scripts/mcp_server.py`
(one block), `scripts/evidence_graph.py`, `scripts/repository_scope.py`,
`scripts/generation_catalog.py`, `scripts/doctor.py` (one docstring),
`tests/test_small_corrections_in_the_generations_area.py`.

## What was found, and what each one gets

- **G-L1 — a row headed by whichever kind was registered last.**
  `_merge_repository_rows` keeps the first row of each checkout, and rows arrive newest
  first. Since one checkout can carry a memory generation and a code generation, the vault's
  row is headed by the memory one after the nightly (`active: True`) and by the code one
  after a daytime refresh (`active: False`). `repository_worktrees.follow_worktree` and
  `unindexed_worktree_root` filter on `active == False`, so whether a worktree of the vault
  can be followed from MCP depended on that order. *Decision:* the row is headed by the
  checkout's newest **code** generation when it has one — every consumer of a row asks a
  code question (which roots it covered, what to follow a worktree with). The catalog's new
  `holds_code` predicate answers the question without reading a file for a modern manifest.
- **G-L2 — `commit_moved` computed and dropped; `stale_by_commit` true for ever.**
  `_staleness` computed `commit_moved` and `_refresh_answer` did not report it. Worse, a
  commit that changes no source leaves the generation's recorded commit behind permanently,
  so `get_architecture`'s freshness block said `stale_by_commit: true` and every new MCP
  process spawned one refresh that found nothing. *Decision:* the refresh answer carries
  `commit_moved`; and the hint table, which every index build and refresh already writes per
  checkout, records the commit its generation was confirmed at — `_current_hints` re-exports
  when the recorded commit is not the current one, and the MCP freshness block treats a
  generation confirmed at the current commit as fresh. No new file and no new state
  directory: `cache/code-hints/` is disposable derived state, and a missing or stale hint
  table only brings back today's answer.
- **G-L3 — the vault captured twice a night.** `_indexed_already` asked
  `detect_repository_changes`, which captures the whole checkout to compare digests, to
  learn whether a generation exists at all. *Decision:* one catalog lookup.
- **G-L8 — three in `evidence_graph.py`.** The row-total ceiling at `_require_row_totals`
  can never fire because each collection is already bounded below it; `_hydrate_reached`
  accepts a deadline and never checks it; `unresolved_calls_naming` reports a `count` taken
  with a different bound than the rows it returns.
- **G-L9 — two in `repository_scope.py`.** An exception raised by a caller's `cancelled()`
  killed the watcher thread, and with it the only bound on a blocking read; a non-canonical
  Windows path raised a bare `ValueError` with no name.
- **G-L11 — a deadline that cancels a commit after the tree is gone.**
  `_write_transaction` applies its busy timeout after the body, which raises `TimeoutError`
  once the deadline has passed — in `discard_superseded` the tree is already removed by
  then. The next prune completes the discard through `_interrupted_discards`, so it heals;
  the docstring now says so instead of claiming nothing past that point can abort.
- **G-L4 — `run_generation_maintenance(code_roots=…)`.** No production caller passes it; 43
  test call sites do. *Decision:* kept, with the docstring saying plainly that it is a test
  seam and that the production path for code is `repository_index`. Removing it would
  rewrite those 43 call sites to drive the builder directly, which is churn in tests that
  pin extractor behaviour, not a product improvement; adding a guard that refuses to
  activate such a generation would change what those tests observe. The hazard the audit
  names — an active generation holding no knowledge — is only reachable by a caller that
  does not exist.
- **G-L5 — a stale comment** in `mcp_server._refresh_action` ("a foreign-repository refresh
  refuses it"), untrue since 2026-09-12. Corrected in place.

## Practice on this date

- SQLite, "Uri Handles": `immutable=1` "is used to indicate that the database file is stored
  on read-only media… SQLite assumes that the file cannot be changed"
  (https://www.sqlite.org/uri.html, fetched 2026-09-17). That is why the confirmation is
  written by republishing the hint file whole, the way `write_hints` already publishes it,
  and never by updating a row inside a file readers open immutable.

## What is not changed

The commit a generation records stays the commit it was built at: it is build-time
provenance, and the identity rule (`RepositoryScope.same_repository`) already ignores it.
