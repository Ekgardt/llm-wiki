# Queue migration planning

`llm_wiki.queue_migration_plan` reads a caller-owned v2 SQLite connection and
returns ordered MigrationStatement values plus row-count summary. Its required
check_owners(connection, tables) callback runs after schema validation and before
task reads. The queue supplies its existing ownership check, including its clock.
Callback failures propagate unchanged. The owner does not open, close or commit a
connection. Caller transaction and admission rules remain authoritative.

Planning preserves payload bytes; a failed payload validation marks the migrated
task dead with payload_hash_mismatch while retaining bytes, hash and history.
Lineage sorting is iterative; missing parents, cycles, duplicate IDs and ambiguous
source identities retain their original refusals. Existing attempt and payload
limits are unchanged. Statement callbacks compare exact rows. Reconciliation also
requires exact migrated table counts and empty v3-only tables. It is not a durable
permit to delete or adopt anything.

Dependencies are canonical byte/payload formats, source identities and SQLite
statement/error values. They do not import the executor. Legacy helper names are
identity aliases until supported consumers cease using them; there are no duplicate
bodies. The planner's private signature now requires explicit ownership policy.
Migration/adoption integration remains with the queue, alongside tests for expired
and live v2 owners. Own tests cover read-only planning, refusal precedence, bound
completion callbacks, long/cyclic lineage and isolated import without connection IO.

Run `uv run pytest tests/modules/queue_migration_plan -q -s`.
See ../research/2026-11-03-queue-migration-plan-boundary.md.
