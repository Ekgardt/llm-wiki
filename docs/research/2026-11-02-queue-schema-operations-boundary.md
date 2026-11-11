# Queue schema inspection and repair on a supplied connection

2026-11-02; completion mandate; baseline df42c0e2.

The new `llm_wiki.queue_schema_operations` owns 14 existing functions:
_normalized_sql, _queue_v3_object_matches, _queue_v3_statements,
_queue_v3_schema_complete, _expected_queue_v3_schema, _queue_schema_rows,
_drop_order_key, _drop_schema_object, _drop_unexpected_objects,
_table_is_populated, _check_no_populated_rebuild, _malformed_queue_v3_objects,
_repair_partial_queue_v3_schema, _queue_v3_trigger_statements; plus the two
existing drop-order/keyword maps. No schema definition is copied: queue_schema
remains the single owner of SQL. Connections are supplied by callers; repair is
an explicit mutating operation, not a read-only or pure parser claim.

The existing `_migration_error` constructor moves to sqlite_contracts; it still
constructs the identical OperationalDatabaseContractError and sets the supplied
code. This lets the new owner avoid importing the executor for an error value.
The existing strict migration identity parser and its wrapper move to
queue_sources, calling its own daily validator instead of MemoryQueue. These two
functions retain migration-specific refusals and remain distinct from first-pair
failure recording. queue_sources now depends on sqlite_contracts, still no IO or
executor. Old queue names are identity aliases; no wrapper state or second body.

`begin_immediate` remains in reliable_memory and is used exactly where it was:
BEGIN IMMEDIATE, body, optional before_commit, commit; on BaseException rollback
and re-raise, retaining a rollback error as context. Neither connection ownership
nor transaction grouping changes. Creation, adoption, migration orchestration,
clock reads, SQL state transitions, current schema fingerprints and compile audit
remain in memory_queue. Existing monkeypatches of queue.begin_immediate exercise
purge/commit paths that remain in queue, not the moved schema-repair operation.

The schema repair keeps refusal of populated malformed tables unless the caller
explicitly allows rebuilding from the migration source. Unreadable tables remain
populated for that decision. Unexpected objects, unsupported kinds and quoted
identifiers retain existing rules and error codes. No public migration executes
against the installed vault as part of this work.

Evidence: current built-in reverse dependency queries and source closure analysis
in logs/queue-schema-operations-{graph,globals}.json. Direct usage search found
one identifier quoting test calling old _drop_schema_object, which remains an
alias. Graph incompleteness is retained; sources/callers/tests were also read.

Research checked 2026-11-02, three independent primary publishers:
- https://www.sqlite.org/atomiccommit.html and https://www.sqlite.org/pragma.html:
  preserve rollback transaction boundaries and actual database contracts.
- https://docs.python.org/3.10/library/sqlite3.html: supplied connection API and
  existing Python 3.10 transaction semantics; no newer autocommit API is added.
- https://martinfowler.com/bliki/BoundedContext.html: ownership by responsibility.
  The completion plan also covers explicit import contracts from Import Linter.
Alternatives: a connection-owning abstraction would change resource ownership;
folding migration/adoption into schema definitions would mix different authority.
Chosen: existing functions on the same supplied connection, exact SQL/AST checks,
independent tests and broader migration/adoption qualification. No new technology,
version, runtime path or arbitrary limit.

## Qualification and dependency correction

The initial complete packet including reliable_memory was 101618 tokens and
failed. The existing begin_immediate primitive was then isolated unchanged in
sqlite_transactions (see its boundary report). Schema operations now import that
owner directly; neither implementation nor isolated import needs reliable_memory.
The unnecessary filesystem source was removed from this packet only after removing
the actual dependency. No tests, required instructions or budget limits were cut.

Baseline migration/identity/quoting: 41 passed. New boundary initially failed
three checks. All 17 moved functions preserve AST except the same-owner source
validator qualification; the remaining queue AST matches after excluding imports
and moved definitions/maps. First owner/related run: 138 passed. Combined final
schema/transaction/consumer qualification: 841 passed, 8 skipped; Python 3.10:
202 passed, 3 skipped. Compatibility and weight completeness: 20 passed.
Global CCN/shape, structure, CI policy, Ruff and diff checks passed. The full suite
running on separate snapshot df42c0e2 does not qualify these subsequent changes.
No native Windows/macOS or live rollout claimed. Exact packet counts are emitted
by the manifest tests and recorded in logs/schema-transactions-all-budgets.json.
