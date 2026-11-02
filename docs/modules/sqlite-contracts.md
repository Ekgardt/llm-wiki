# SQLite value contracts

`llm_wiki.sqlite_contracts` owns OperationalDatabaseContract,
OperationalDatabaseContractError, MigrationStatement, their validators, coded migration-error construction and exact
SQLite identifier quoting. It imports only standard library modules and opens no
files or databases. There is one definition of every type, default and validator.
All connection handling, PRAGMAs, migration execution and durability remain in
reliable_memory. General SQL identifier quoting is not SQL value interpolation.

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
