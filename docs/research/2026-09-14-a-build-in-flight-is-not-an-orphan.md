# A build in flight is not an orphan

Dated 2026-09-14. Item 2.4 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `doctor._repair_generation_catalog` (nightly maintenance and `doctor --repair`)
  removes every child of `cache/evidence-graph/generations/` that the catalog does not
  register and that fails `generation_catalog._validate_generation`
  (`_removable_generation_orphan` → `shutil.rmtree`).
- A generation is built in place: `evidence_graph_builder._created_generation_directory`
  creates the directory, the builder fills it, and only then is it sealed and
  registered. Until that moment a build in progress is exactly "unregistered and
  invalid". `generation_catalog._require_activated` says the same for registrations:
  "a build in flight looks exactly like an abort".
- The repair runs under the `doctor:maintenance` fence. The same catalog
  (`cache/evidence-graph/catalog.sqlite3`) is written by the repository refresh under
  its per-repository refresh fence (`repository_index.run_fenced`), by
  `repository_index.index_repository` from MCP, and by the benchmark vaults. The fences
  do not exclude each other. The audit reproduced both leases granted and the
  in-progress directory deleted.
- The code graph: `_created_generation_directory` ← `build_full_generation`,
  `build_incremental_generation` ← `doctor._build_or_refresh_generation`,
  `repository_index._build`, benchmark builders; `_repair_generation_catalog` ←
  `doctor` repair paths only.
- The longest builder bound in this codebase on this date is 15 minutes
  (`NIGHTLY_GENERATION_BUDGET_SECONDS`, `repository_index.REFRESH_ALL_BUDGET_SECONDS`);
  a builder writes into its directory as it goes.

## Practice on this date

- Git's garbage collector does not delete unreachable objects younger than a grace
  period (`gc.pruneExpire`, default two weeks) precisely because a concurrent process
  may be about to reference them, and temporary pack files are only removed once stale
  ([git-gc, gc.pruneExpire](https://git-scm.com/docs/git-gc#Documentation/git-gc.txt-gcpruneExpire)).
  A collector that cannot see a writer's intent waits out any write that could still be
  live.
- The alternative, a lock every builder takes, needs every writer — including
  benchmarks and older checkouts — to cooperate before it protects anything.

## The decision

- An unregistered, invalid generation directory is removed only when nothing in it —
  the directory itself or any of its immediate entries — has been modified for
  `GENERATION_ORPHAN_GRACE_SECONDS = 24 h`, far above the 15-minute builder bound.
  A younger one is left and removed by a later repair.
- The existing repair test ages its partial directory; a new test shows a fresh partial
  directory survives the repair.

Why not the alternatives:

- **A shared build fence.** Correct in principle, but it changes every builder's
  admission and the fences' documented roles; the grace period is enough for a
  directory no build has touched for a day.
- **Skip cleanup entirely.** Leaves every aborted build on disk forever.

Files: `scripts/doctor.py`, `tests/test_generation_maintenance.py`,
`tests/test_a_build_in_flight_is_not_an_orphan.py`,
`docs/research/2026-09-14-a-build-in-flight-is-not-an-orphan.md`.
