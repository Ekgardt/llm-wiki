# SQLite value contracts without filesystem IO

2026-11-02, completion mandate. Baseline a49f7a81.
Move OperationalDatabaseContract, OperationalDatabaseContractError,
MigrationStatement, their existing `_require_positive_int` and
`_require_migration_text` validators, and `quote_sqlite_identifier` from
reliable_memory into `llm_wiki.sqlite_contracts`.

The module is stdlib-only. It owns immutable value types, their validation,
error identity, and exact identifier quoting; it does not open databases,
change PRAGMAs, execute migrations or own filesystem state. Those operations
remain in reliable_memory. User version 3 and every validation/error are unchanged.
Old reliable_memory names are aliases. Classes retain `__module__` there to
preserve old pickle bytes and historical imports, as queue_contracts already does.
There is no second class, validator or independent mutable state.

Consumers of the moved symbols import their owner directly; other imports from
reliable_memory remain. Five production consumers import the three classes:
installed_memory_repair, operational_ownership, compile_progress_migration,
markdown_transaction, memory_queue. Identifier consumers are separately scanned.
Tests check frozen types, exact errors, callback rules, identifier escaping,
old/new object identity, pickle byte preservation, and import without runtime IO.
Queue/transaction/migration and reliable_memory qualification exercises the actual
operations in addition to these isolated tests.

Research: the completion plan's three independent primary sources were checked
2026-11-02. The value contract is the smallest boundary needed; a Protocol,
abstract store, new database adapter or second transaction owner is unnecessary.
Keeping errors inside the filesystem implementation makes pure schema/validation
consumers load IO. Moving SQL execution together would exceed this responsibility.
Python >=3.10, sqlite3, dependency versions, schemas and runtime layout unchanged.

Baseline evidence: logs/sqlite-contracts-baseline.json records exact protocol-4
pickle bytes/repr before moving; logs/sqlite-contracts-baseline-tests.txt;
logs/sqlite-contracts-consumers.json. Graph findings and post-change results follow.

## Results

Built-in dependency traversal read generation-18e45d078725e66a-2962d838;
all three queried symbols resolved, with the graph's explicit incompleteness.
Baseline: 77 passed, 3 skipped. New boundary before extraction: 3 failures.
All six moved definitions preserve exact AST; the remaining implementations of
reliable_memory and five production consumers match after excluding imports.
Protocol-4 historical pickle bytes and repr remain identical.
Expanded qualification: 562 passed, 8 skipped, including queue/migration,
transaction/recovery, ownership, compile-progress migration, global complexity,
structure and CI policy. Python 3.10 selection: 148 passed, 3 skipped.
Own/compatibility weighted tests: 47 passed. Budget and weight completeness:
9 passed. Ruff and git diff checks clean. No native Windows/macOS run or rollout.
Evidence: logs/sqlite-contracts-{graph.json,preservation.json,related.txt,
python310.txt,weights.txt}. Final context test emits the exact current count.
