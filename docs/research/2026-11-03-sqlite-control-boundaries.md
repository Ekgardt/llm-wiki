# SQLite deadlines and resumable statement execution

2026-11-03; approved completion mandate. Two independent responsibilities:

- llm_wiki.sqlite_deadlines owns operational_deadline_active, operational_busy_ms,
  configure_operational_deadline, refresh_operational_deadline and the existing
  _OPERATIONAL_PROGRESS_VM_STEPS constant with its measured rationale. It operates
  on a supplied connection and uses the same time.monotonic module object. No new
  timer, thread, connection or timeout. It preserves cooperative interruption,
  finite/None/infinite behavior and SQL progress callback frequency.
- llm_wiki.sqlite_migrations owns _migration_incomplete,
  _require_unique_statement_names, _apply_one_migration_statement,
  _require_final_invariant and run_resumable_migration. Dependencies are only
  SQLite statement/error contracts and the existing transaction contextmanager.
  It executes the same one-statement transaction, verifies the same completion
  invariants and emits killpoints at the same stages. No new database owner,
  migration ledger, schema policy, global transaction or exception suppression.

Existing names in reliable_memory remain identity aliases with the public
function pickle module names retained. Consumers import the new owners directly
at their existing lexical scope; test patch points in consumers remain intact.
No second function body remains. Alias removal requires evidence that supported
callers no longer use the old API. Existing real-file migration crash tests remain
integration tests; independent owner tests use SQLite directly, so unit context
needs no filesystem implementation.

Research checked 2026-11-03, three independent primary publishers:
- https://www.sqlite.org/atomiccommit.html and
  https://www.sqlite.org/c3ref/progress_handler.html: rollback semantics,
  one connection-scoped progress callback, nonzero callback interruption.
- https://docs.python.org/3.10/library/sqlite3.html and
  https://docs.python.org/3.10/library/time.html#time.monotonic: existing APIs and
  monotonic clock; preserve project Python 3.10 compatibility despite upstream
  end of support. This refactor does not change the supported-version contract.
- https://martinfowler.com/bliki/BoundedContext.html: separate explicit ownership
  by responsibility. These boundaries are internal libraries, not services.

Alternatives: leave control in filesystem IO, move all operational storage at once,
introduce a connection-owning class, or use newer autocommit APIs. The first keeps
unnecessary coupling; the others expand behavior and lifecycle changes. Selected
exact-function extraction is sufficient. No dependency or arbitrary limit is added.

Source inspection: seven production consumers, including one deadline-only use
in compile_progress. Existing tests patch reliable_memory.time.monotonic; time is
still the identical standard-library module. Graph qualification is required before
implementation; evidence is recorded in logs/sqlite-control-graph.json.
Baseline operational IO/migration and compile deadline consumers: 258 passed,
3 skipped. Full regression on earlier df42c0e2 found two unrelated documented
transfer omissions; those are repaired separately, not hidden by this boundary.

## Qualification

Built-in graph refreshed at dc471870 (generation-18e45fe582d8e2bb-9098c245);
reverse queries found 11 migration, 87 deadline, 5 configuration references.
Graph gaps were supplemented by source and import inspection. All nine moved
function ASTs match exactly; eight remaining source files match except imports
and removed definitions/constant. Five public function protocol-4 pickle byte
sequences are identical to their preimages. New boundary guards first failed six
checks. Initial own/related tests: 275 passed, 3 skipped. Expanded qualification:
563 passed, 4 skipped, including global complexity/shape, structure, CI policy,
limit rationale and privacy. Additional archive/compile/weight checks: 90 passed.
Python 3.10: 281 passed, 3 skipped. Test weights measured: 25 passed. Ruff and
diff checks passed. All 27 complete maintenance packets fit; exact current counts
are recorded in logs/sqlite-control-all-budgets.json. Full regression on this
combined candidate remains required; no native Windows/macOS or live rollout.
