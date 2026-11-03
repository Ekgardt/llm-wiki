# Queue database compatibility

`llm_wiki.queue_database_contract` owns the existing v3/compile database contract
objects, their exact schema hashes, version selection and compatibility checks.
It does not open a path, initialize a database, choose transactions, migrate data,
run a worker or audit all queue invariants. Those responsibilities retain their
existing owners. No environment setting or operational version changes.

A supplied connection must have a supported user_version and matching application
identity. The compile version additionally requires the exact protected journal
objects. An explicit expected contract is also enforced. The checked-connection
function returns that same connection on success and closes it on any failure,
including BaseException subclasses; the original exception behavior is unchanged.
The existing path-opening wrappers remain in memory_queue and use this checker.

Hash inputs retain the original queue table/index/trigger declarations, application
and user version, plus the compile version and exact journal SQL. Golden fixtures
captured before extraction bind those values. memory_queue retains aliases to the
same objects/functions for migrations, callers and operator commands. Keep them
until consumers and compatibility have been deliberately migrated.

Tests exercise real SQLite versions, missing/altered protected objects, identity
mismatches, caller transaction retention and failure closure. Isolated import
checks refuse queue and journal executors. Old facade assertions and migration
scenarios remain separate integration obligations; packet qualification does not
replace native Windows/macOS or whole-project tests.

Shared canonical contract: [Compile journal schema](compile-journal-schema.md).
Source: [database boundary research](../research/2026-11-03-queue-database-contract-boundary.md).
