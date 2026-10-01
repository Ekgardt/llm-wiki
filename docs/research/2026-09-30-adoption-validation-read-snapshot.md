# Validate a database in one read transaction

Date: 2026-09-30. Local runtime: Python 3.14.6, SQLite rollback journal.

A read-only profile of the live capture admission path took 2.947 seconds:
adoption retried 16 times, including 15 retry waits. The validation connection
ran separate autocommit reads. A concurrent writer entering pending commit
between schema and integrity reads could refuse the next read and restart the
entire validation. The hook delegate has an existing 3.5-second deadline.

Four deterministic regressions recreate the pending writer using real SQLite
connections, with no sleeps: both adoption databases and both direct validators.
An existing reader holds a shared lock, a writer's commit reaches SQLITE_BUSY,
and the validation must retain its own earlier read view. All four tests fail
on the old code with `database is locked` after the schema query.

Primary sources reviewed:
- [SQLite isolation](https://www.sqlite.org/isolation.html): explicit read
  transactions keep a consistent view; rollback-journal writers need readers
  to release their locks before commit.
- [Python sqlite3 transaction control](https://docs.python.org/3/library/sqlite3.html#transaction-control):
  SELECT does not implicitly begin a transaction in legacy transaction control;
  explicit SQL controls the transaction when automatic control is disabled.
- [APSW tips](https://rogerbinns.github.io/apsw/tips.html): transactions group
  multiple operations and contention must be handled deliberately. APSW is a
  reference, not a new dependency.

Decision: BEGIN on the existing read-only connection before schema, integrity,
foreign-key, logical-invariant and metadata checks; closing the connection
releases the read transaction. Apply the same rule to adoption and direct
queue/coordinator certification. No check is removed and no hook timeout is
increased. A writer already pending before admission can still cause a retry.

Alternatives: longer hook deadlines conceal repeated scans; removing integrity
checks weakens the current contract; WAL changes the storage contract and is
unnecessary. The chosen approach holds a shared lock across the finite database
validation, delaying writer commit for that interval, but avoids repeating the
whole scan and never holds it during LLM or filesystem publication work.

This proves the reproduced contention defect is fixed. It does not prove every
historical timeout represents a loss or that no other timeout cause exists.
