# SQLite value contracts

`llm_wiki.sqlite_contracts` owns OperationalDatabaseContract,
OperationalDatabaseContractError, MigrationStatement, their validators, coded migration-error construction and exact
SQLite identifier quoting. It imports only standard library modules and opens no
files or databases. There is one definition of every type, default and validator.
sqlite_io owns SQLite connection lifetime and lock probes. Runtime filesystem
policy, records, fingerprints, checked sync and publication have separate owners.
sqlite_pragmas owns settings/header policy; sqlite_migrations owns resumable
statement execution; sqlite_deadlines owns cooperative deadline control. These
owners consume the value contracts directly. General SQL identifier quoting is
not SQL value interpolation.

reliable_memory re-exports the same objects. Their historical `__module__` stays
reliable_memory so existing serialized values retain exact pickle lookup paths.
Those aliases cannot be removed while historical serialized values are supported.
No duplicate implementation is retained. Direct production consumers import the
new owner; integration tests verify this, class identity and old pickle bytes.

The isolated packet includes every own test and fixture; it does not import the
queue, filesystem implementation, transaction coordinator or installer. Tests
cover immutable value objects, exact validation, callback semantics, identifier
escaping and side-effect-free import. Broader operational tests separately check
integration with actual migrations, ownership and transaction recovery.

Run `uv run pytest tests/modules/sqlite_contracts -q -s`.
Source: ../research/2026-11-02-sqlite-contracts-boundary.md.

## Canonical ownership reference

`llm_wiki.sqlite_contracts` owns the existing operational database contract,
migration statement and contract error types, their value validators, and exact
SQLite identifier quoting. It imports only stdlib. reliable_memory retains identity aliases; sqlite_io owns connection lifetime and
the runtime filesystem owners own file behavior. sqlite_pragmas, sqlite_migrations
and sqlite_deadlines own their named control policies, with direct aliases for
the old names. Class pickle names remain reliable_memory for existing
serialized callers. Consumers import values directly from sqlite_contracts.
