# The JSON queue import goes

Dated 2026-09-23. Second stage of clearing legacy, after
`docs/research/2026-09-23-legacy-that-nothing-reads.md`. This note removes the import
of the file-per-task JSON queue (`run/queue/*.json`, `*.processing`) that releases
v3.3.0 through v3.4.0 (2026-07-03 to 2026-07-11) wrote, and everything that only
exists to read, repair, migrate, quarantine, or mark that queue.

Files: `scripts/memory_queue.py`, `scripts/doctor.py`, `scripts/archive_daily.py`,
`scripts/memory_state.py`, `install.sh`, `install.ps1`, `CLAUDE.md`, `AGENTS.md`,
`docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`,
`tests/test_memory_queue_migration.py`,
`tests/test_a_reimported_legacy_record_is_not_a_conflict.py` (deleted),
`tests/test_memory_queue_cli.py`, `tests/test_doctor.py`,
`tests/test_archive_daily_bagit.py`, `tests/test_runtime_deletion_contract.py`,
`tests/test_the_json_queue_import_goes.py`,
`docs/research/2026-09-23-the-json-queue-import-goes.md`.

## What was found

- The JSON queue was replaced by the SQLite queue in commit 299b34ac on 2026-07-14.
  Every tag from v3.3.1 to v3.4.0 still holds the JSON queue; v4.0.0 (2026-08-25),
  the current release, holds the SQLite queue and none of the JSON code paths.
- The import is not an automatic upgrade path today. Measured on this date with a
  scratch state root holding one July record under `run/queue/`: the installer's
  adoption check answers `conflict` with `legacy_operational_evidence_present:
  run/queue`, the apply step answers `reliability_v3_adoption_failed`, and the record
  stays where it was. Nothing in `install.sh` or `install.ps1` runs the import; it runs
  only when a v2 client enqueues on an unadopted vault, on `doctor --repair` of an
  unadopted vault, or on `memory_queue.py migrate`. A July vault therefore reaches
  V3 today only by a manual sequence the installer does not name.
- The live vault adopted V3 on 2026-08-26 and has no `run/queue/`; its
  `queue-migrated-v2` marker dates from 2026-08-20 and is read by nothing on an
  adopted vault (`doctor` reports the migration as `retired`).
- Both installers still create an empty `run/queue/` on every install (`install.sh`
  line 487, `install.ps1` line 410). Adoption tolerates an empty directory; the
  directory has had no writer since 2026-07-14.
- What exists only for that queue: in `memory_queue`, the marker writer and
  validator, the bounded no-follow record reader, the record validator and importer,
  the quarantine, the post-marker conflict, the migration guard and owner roles, the
  `migrate` command and `MigrationReceipt` — about 560 lines; in `doctor`, the
  per-file scan of pending, failed and stale `.processing` leases, the lease repair,
  the marker accounting (`migration: pending|complete|conflict`) and the migration
  repair action; in `archive_daily`, the reader that blocks a daily archive on a JSON
  task that names it; 33 tests in `test_memory_queue_migration.py`, of which 13 are
  about the import itself, and one whole file about re-importing after a crash.

## Practice on this date

- Dead code is removed, not commented or flagged; the removal is safe when nothing
  reaches it, which the callers and the installer's measured behaviour establish
  ([Martin Fowler, Remove Dead Code](https://refactoring.com/catalog/removeDeadCode.html)).
- A refusal beats a silent drop: a vault that still holds JSON records must be told
  so, not migrated by a path the installer never takes and not emptied. The
  existing adoption refusal already names `run/queue`.

## The decisions

1. The import goes. `run/queue/` holding entries is a refusal, in one place on each
   side: the queue refuses to open the v2 backend with `legacy_json_queue_unsupported`,
   and `doctor` reports the directory's entry count as a degraded queue with the
   deletion code `legacy_queue_retained` so `run/` deletion stays blocked while the
   records exist. Adoption keeps refusing on the same evidence.
2. Nothing writes or validates `queue-migrated-v2` any more. An existing marker is
   ignored by the queue and by `doctor`; adoption still lists it as legacy evidence
   because a marker without a database pair is what an earlier release left behind.
3. The v2 queue database is created by `MemoryQueue` on first use, as it already was;
   `_ensure_sqlite_enabled` keeps only the adoption short-circuit and the refusal.
4. `doctor --repair` on an unadopted vault repairs nothing about the JSON queue; the
   `migrate_queue` repair action and the `.processing` lease repair go with it.
5. The installers stop creating `run/queue/`.
6. The remaining stage — the v2 SQLite queue and coordinator readers, the retired
   databases and their tombstones — stays behind its own note; it changes what an
   unadopted vault can do at all.

## Cost, by rule 4

Fewer directory scans on the queue check of an unadopted vault; no runtime cost added.
An upgrade from a July release changes from an undocumented manual sequence to a
documented refusal that names the directory.

## Sources

- [Remove Dead Code — Refactoring catalog](https://refactoring.com/catalog/removeDeadCode.html) — fetched 2026-09-23.
- `git merge-base --is-ancestor 299b34ac <tag>` over every tag; `git show v3.4.0:scripts/memory_queue.py` (the July record schema), 2026-09-23.
- `scripts/repair_installed_memory.py --check|--apply` against a scratch state root holding one July record (a scratch script under the job's temporary directory, not kept), 2026-09-23.
- `run/` on the live vault (listing only), 2026-09-23.
