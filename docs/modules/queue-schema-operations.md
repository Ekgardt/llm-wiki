# Queue schema inspection and repair

`llm_wiki.queue_schema_operations` constructs the existing schema migration
statements, checks catalog definitions and explicitly removes partial/unexpected
objects when the caller's existing policy permits it. It operates on a supplied
SQLite connection. It does not open a database, adopt a queue or verify task data.

queue_schema owns the unchanged SQL definitions. sqlite_contracts owns statement
values, identifier quoting and the coded migration error. sqlite_transactions owns
`begin_immediate(connection, before_commit=None)`: begin, yield, optional commit
check, commit; on BaseException roll back and re-raise, retaining rollback errors
as context. Schema repair calls this contract for the same single-object drops
as before. There is no new connection, transaction grouping or commit policy.

An unreadable table counts as populated. Populated malformed tables are refused
unless the caller explicitly allows rebuilding from its migration source.
Unsupported object kinds remain refusals even with rebuild permission. Drop order
is triggers, indexes, tables; every identifier is quoted exactly. Definition
comparison uses [schema text](sqlite-schema-text.md), preserving literals and
quoted identities while normalizing outside-token ASCII formatting. Creation callbacks are
bound to the correct definition, and trigger installation is a separate phase.

Old queue function/constant names are identity aliases for existing internal callers
and identifier tests. No second SQL or function body is retained. Unit tests use
real in-memory SQLite, including a failure after DROP that proves DDL rollback.
Migration/adoption integration additionally checks the owning runtime pipeline.

Run `uv run pytest tests/modules/queue_schema_operations -q -s`.
Source: ../research/2026-11-02-queue-schema-operations-boundary.md.

## Canonical ownership reference

`llm_wiki.queue_schema_operations` owns schema inspection and explicit repair on
a supplied SQLite connection. SQL definitions remain in queue_schema; transaction
boundaries and connection/adoption ownership are unchanged. sqlite_contracts owns
the shared migration-error value constructor; queue_sources also owns strict
migration identity parsing. None imports the queue executor.
