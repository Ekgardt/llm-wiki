# The generation is the only index

Dated 2026-09-23. Third and last stage of clearing legacy, after
`docs/research/2026-09-23-legacy-that-nothing-reads.md` and
`docs/research/2026-09-23-the-json-queue-import-goes.md`. This note retires the legacy
FTS5 index (`cache/index.sqlite`, `cache/.paths-manifest`) and the legacy vector cache
(`cache/vectors.npy`, `cache/vectors_meta.json`) and makes the evidence generation the
one thing a search reads, with a bounded direct read of Markdown as the only fallback.

Files: `scripts/search_memory.py`, `scripts/retrieval.py`, `scripts/doctor.py`,
`scripts/sync_memory.py`, `scripts/scheduled_nightly.py`, `scripts/lookup_mode.py`,
`CLAUDE.md`, `AGENTS.md`, `docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, `docs/ARCHITECTURE.md`,
`README.md`, `README.ru.md`, `README.zh-CN.md`, `CHANGELOG.md`,
`tests/test_search_ranking.py`, `tests/test_doctor.py`, `tests/test_sync_memory.py`,
`tests/test_retrieval_review_round2.py`, `tests/test_retrieval_review_blockers.py`,
`tests/test_retrieval.py`, `tests/test_mcp_server.py`, `tests/test_mcp_contract.py`,
`tests/test_structure.py`, `tests/test_runtime_deletion_contract.py`,
`tests/test_workspace_revision.py`, `tests/test_repository_scope.py`,
`tests/test_the_generation_is_the_only_index.py`, and the other test files that name
`cache/index.sqlite` only as a fixture path,
`docs/research/2026-09-23-the-generation-is-the-only-index.md`.

## What was found (measured 2026-09-23)

- On the live vault the legacy index answered 537 of 15 284 retrievals ever, the last on
  2026-09-05; since then every answer came from a generation
  (`cache/evidence-graph/telemetry.sqlite3`, read-only).
- A fresh vault reads the legacy index by design: `doctor`'s `generation` check answers
  `ok` with "Evidence generation has not been built; legacy retrieval remains available"
  (`repairable: true`), so the installer's `sync_memory.py --apply` refreshes no
  generation and rebuilds only `index.sqlite`. Measured against a scratch state root
  from the tasks worktree: the sync's `indexes` action reported "Derived search index
  was rebuilt", `cache/` held `index.sqlite` and no `evidence-graph/`.
- A full generation build of the live vault's 189 pages, vectors included, took 48.9 s
  into a scratch state root (`doctor.run_generation_maintenance`, `force_rebuild`). A
  generation without vectors is a supported state (`vector_state: absent`, published by
  the same builder when the `hybrid` extra is missing); PyYAML is a base dependency
  since 2026-09-14 (`docs/research/2026-09-14-a-base-install-can-search.md`).
- Two backends exist for every signal: `retrieval.py` reads the generation when one is
  active and otherwise `search_memory._legacy_lexical_hits` (an FTS5 index rebuilt
  lazily from Markdown, with a direct Markdown read when SQLite fails) and
  `_legacy_dense_hits` (a numpy matrix beside the index). `doctor` carries an `index`
  check and repair for the legacy index, `sync_memory` an index builder, the nightly a
  Step 3b that rebuilds it, `lookup_mode` a probe of its age, and `search_memory` the
  `--rebuild` and `--status` commands for it. About ninety functions in `search_memory`
  are reachable only from those entry points.
- The two retrieval review test files and 30 of the 78 ranking tests exercise the
  legacy index itself (its swap lock, its freshness manifest, its numpy contract); the
  rest exercise the generation and the shared ranking.

## Practice on this date

- One retrieval path with one honest fallback is the norm for a local-first index: a
  derived index is rebuilt from the source of truth, and a reader that cannot find the
  index reads the source, it does not maintain a second index
  ([Martin Fowler, Remove Dead Code](https://refactoring.com/catalog/removeDeadCode.html);
  the same rule this vault applies to `cache/` as "disposable and derived",
  `docs/STRUCTURE.md`).
- The fallback must say so: every hit read directly from Markdown carries
  `fallback_reason: no_active_generation` and the answer is marked partial, as the
  legacy direct read already did with `legacy_sqlite_unavailable`.

## The decisions

1. The legacy FTS5 index and the legacy vector cache go: their builders, readers, swap
   lock, freshness manifest, `doctor`'s `index` check and repair, `sync_memory`'s index
   builder, the nightly's Step 3b, `lookup_mode`'s index probe.
2. Without an active generation a search reads Markdown directly, bounded by the
   caller's deadline, marks the answer partial with `no_active_generation`, and runs no
   dense or graph signal. `doctor`'s `generation` check reports a missing catalog as
   `degraded` and repairable, so the installer's sync builds the first generation, as
   the nightly already refreshes it.
3. `search_memory.py --rebuild` rebuilds the generation through
   `doctor.run_generation_maintenance` under the maintenance fence; `--status` names
   the active generation and the page count.
4. Existing `cache/index.sqlite`, `cache/.paths-manifest`, `cache/vectors.npy` and
   `cache/vectors_meta.json` are read by nothing and may be deleted; nothing deletes
   them automatically (`cache/` is disposable already).
5. The v2 SQLite queue and coordinator readers stay: v4.0.0, the current release,
   ships them, and the upgrade to V3 reads them (`installed_memory_repair`).

## Cost, by rule 4

A fresh install spends the generation build (under a minute here, seconds without
vectors) inside the sync it already runs; a search before that reads Markdown directly
and says so. Every later search reads one index instead of choosing between two.

## Sources

- [Remove Dead Code — Refactoring catalog](https://refactoring.com/catalog/removeDeadCode.html) — fetched 2026-09-23.
- `cache/evidence-graph/telemetry.sqlite3` (read-only), `logs/`, `run/` on the live vault; `doctor.run_generation_maintenance` and `sync_memory.py --apply --json` against scratch state roots, 2026-09-23.
