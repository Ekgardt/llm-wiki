# Queue migration planning on supplied connections

2026-11-03; approved completion mandate; baseline 8a434963.

Move the existing v2 schema validation, task lineage ordering, byte-preserving
payload conversion, SQL statement planning and exact reconciliation functions to
llm_wiki.queue_migration_plan. The selected names are listed below. No database
is opened, committed, adopted or repaired by this owner. Existing input bounds,
SQL, mismatch disposition, source ambiguity refusals and ordering stay unchanged.
The existing _still_held and _require_unambiguous_v2_owners remain in memory_queue;
the planner receives the latter as a required check_owners callback, invoked at
the same point after schema validation and before reading tasks. This preserves
clock calls and refusal precedence without a backward import. Its only caller
passes the existing function explicitly. No optional no-op or default bypass.
Other old helper names remain identity aliases for compatibility; no duplicate
implementation. Remove aliases only after repository and supported callers no
longer use them. The changed private planner signature has no other source caller.

Dependencies: stdlib, formats canonical/payload, queue_sources, sqlite_contracts.
IO/adoption, source opening, ownership policy and transaction execution remain in
memory_queue. The packet includes dependency source/contracts and pytest bootstrap.

Research checked 2026-11-03, three independent primary publishers:
- https://www.sqlite.org/atomiccommit.html — preserve existing rollback transaction
  boundaries; planning must not acquire a second connection or publish data.
- https://docs.python.org/3.10/library/sqlite3.html — supplied connection and row
  semantics. Python 3.10 is upstream unsupported but remains the project minimum;
  this work does not silently upgrade it or use newer connection APIs.
- https://martinfowler.com/bliki/BoundedContext.html — explicit responsibility and
  relationships. Chosen internal boundary is a plan, not a new service.
Alternative clock injection would move owner policy too; a new repository class
would add lifecycle ownership. Neither is needed to isolate planning. No new
version, dependency, limit, public command or runtime path.

Evidence: fresh index generation-18e45f6c1720bccc-e3786d01 and built-in reverse
queries in logs/queue-migration-plan-graph.json, supplemented with direct source
and test call-site inspection because graph completeness is not assumed.

## Selected definitions

_queue_v2_tables, _QUEUE_V2_SCHEMA_OBJECTS, _validate_queue_v2_schema_objects, _queue_v2_columns, _queue_v3_row_matches, _queue_v3_insert_statement, _QUEUE_V2_REQUIRED_TASK_COLUMNS, _QUEUE_V3_TASK_COLUMNS, _QUEUE_V2_HISTORY_COLUMNS, _QUEUE_V2_FAILURE_COLUMNS, _require_queue_v2_tasks_schema, _queue_v2_child_counts, _already_ordered, _parents_before_children, _place_with_parents, _redrive_chain, _ordered_queue_v2_tasks, _migrated_payload_bytes, _migrated_attempts, _migrated_task_values, _task_migration_statements, _attempt_history_statements, _task_source_link_statements, _source_failure_migrated, _source_failure_statement, _source_failure_statements, _queue_v2_migration_statements, _QUEUE_V2_MIGRATED_TABLES, _QUEUE_V3_ONLY_TABLES, _tables_have_counts, _queue_v2_reconciliation_complete

## Qualification

Baseline migration/adoption tests: 53 passed. New boundary tests first failed
three checks. Source comparison: 30 of 31 moved definitions unchanged; only the
planner receives the required callback. The remaining queue AST is identical
except imports and passing the unchanged ownership function. First own/related
run: 64 passed. Expanded queue/migration/adoption, global complexity/shape,
structure and CI-policy checks: 432 passed. Python 3.10: 80 passed. The new test
guard initially exceeded CCN; it was split into import and lifecycle assertions
without removing either check. Final own/weight completeness: 20 passed; measured
weights: 12 passed. Ruff and diff checks passed. All 25 complete packets pass;
exact current counts are in logs/queue-migration-plan-all-budgets.json.
The full suite on df42c0e2 is still separate; no full-current or platform-native
Windows/macOS qualification and no live installation are claimed.
