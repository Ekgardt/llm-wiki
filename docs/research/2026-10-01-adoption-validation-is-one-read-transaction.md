# Adoption validation is one read transaction

Research date: 2026-10-01. Installed diagnostics retain 303 post-tool capture failures between 08:03:04 and 08:58:53 with a wrapped SQLite database-is-locked cause. This does not establish that all 303 failures have the same interleaving or are recoverable. Source/native graph inspection finds adoption validation checks schema, integrity, foreign keys and metadata through an autocommit read-only connection. A writer can enter between those checks.

A deterministic real-SQLite regression introduces a competing exclusive writer after the real schema check, before the remaining checks. Both queue and coordinator validation fail on the old code at PRAGMA integrity_check with SQLITE_BUSY. The test preserves the actual adopted databases and all real validation; only the scheduling point is controlled. No sleeps, fabricated successful validation or loss-counter reclassification are used.

Begin one explicit deferred read transaction before the schema check. All remaining checks then observe the same database under its read lock; closing the read-only connection releases it on success or exception. Existing whole-adoption retries still handle contention before admission. A writer must wait for this validation transaction to finish. This does not eliminate every possible busy condition, shorten the admitted full integrity check or prove existing capture losses recovered.

Three independent primary references verified today:

- [SQLite isolation](https://www.sqlite.org/isolation.html): rollback-journal read transactions isolate readers while exclusive writers are serialized.
- [SQLAlchemy SQLite transaction control](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html): Python legacy SELECT behavior does not automatically establish a repeatable read transaction. The fetched 2.0 documentation identifies itself as legacy; this project uses neither that library nor its version-specific API.
- [Microsoft.Data.Sqlite transactions](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/transactions): a deferred transaction takes its read protection when its first read executes, and contention before admission remains possible.

The installed qualification runtime is Python 3.12.3 with SQLite 3.45.1. Plain SQL BEGIN is compatible with the project's Python 3.10 minimum. Keep rollback journal, synchronous FULL, read-only opening, complete schema/integrity/foreign-key checks, physical identity checks and fail-closed ownership. No new schema, setting, limit, process or storage location is introduced.

Alternatives: a larger retry count or sleep does not repair interleaving between checks; removing full validation violates the operating contract; WAL is explicitly unsupported by this runtime; a new snapshot store or lock protocol adds unnecessary architecture. One existing SQLite read transaction supplies the required coherence with the tradeoff that competing writers cannot commit until it closes.

Evidence: logs/audit-2026-10-01-adoption-snapshot-{architecture-before.json,red.txt}; logs/audit-2026-10-01-failure-kinds-doctor.json. Expected full-cycle benefit is avoiding repeated full validation and lost producer work from this interleaving. No model call, token cap or content reduction is added; broader real contention remains an installed qualification question.

Qualification: 138 candidate checks and 182 public-source checks passed, including both real interleaving regressions and existing adoption/capture/whole quality guards. Fresh Gitleaks found no leaks. Ruff passed after correcting test import order; real Lizard measured the changed production function CCN 1 and test/helpers <=3. The installed compatible source update used canonical repair admission, atomic replacement and verified preimages; existing unknown owners and protected evidence were retained. Complete validation of the actual installed adoption succeeded in 0.527 seconds. This is one successful admission, not proof that every future contention or all prior capture loss is resolved.
