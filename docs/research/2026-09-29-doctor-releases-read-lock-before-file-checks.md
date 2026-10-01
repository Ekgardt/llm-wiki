# Doctor releases its SQL read lock before checking files

Date: 2026-09-29. No journal mode, durability, ownership, timeout, schema,
runtime location or supported-version changes.

## Reproduced defect and dependencies

`doctor._transaction_check` opens the operational database read-only, reads
transactions and operations in one SQL snapshot, then checks undo artifacts
and reports health. Previously that same read transaction remained open while
walking the filesystem and checking each retained undo directory. In rollback
journal mode its shared lock prevented another connection from committing.
A regression makes an independent writer commit at the artifact-inspection
boundary, with no sleeps. The old code fails with a locked database and an
unreadable-state report; the new code permits the commit.

Hook breadcrumbs use `append_daily` -> `append_knowledge` -> the transaction
coordinator against that database. Their deadlines are shorter than the host
hook deadline. This lock lifetime is a confirmed source of writer contention,
not proof that it caused every historical capture timeout. Durable lifecycle
intents and breadcrumb durability remain different contracts.

## Current primary sources and choice

- [SQLite locking in rollback mode](https://www.sqlite.org/lockingv3.html)
  explains why shared readers delay the exclusive lock needed for commit.
- [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)
  documents connection transactions, cursor reads and materialized rows.
- [Microsoft.Data.Sqlite transactions](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/transactions)
  documents isolation and lock conflicts in concurrent transactions.

These independent primary sources support shortening the read transaction
without abandoning snapshot isolation. They do not establish a universal
latency guarantee; project tests and local measurements qualify this change.

Rejected alternatives: removing the SQL snapshot can falsely report missing
operations during a concurrent commit; increasing hook deadlines conflicts
with host budgets; WAL changes the installed durability/compatibility contract;
copying the entire database duplicates unrelated tables and state. Streaming
validation while holding the original read lock retains the demonstrated bug.

The reader now obtains every required row, operation position and error code
in the same SQL snapshot, releases it, then validates the captured rows and
filesystem. Error-code lookups do not silently switch to newer transaction
rows. No sample, row cap or integrity check is removed. Copied-row validation
still checks the same absolute deadline. Existing deletion admission remains
authoritative: a health read is never a deletion permit or an atomic snapshot
of the filesystem.

## Cost and checks

Transaction metadata is retained for the duration of this read, alongside
the existing ID and operation-position collections. Memory therefore grows
with actual metadata, without a new arbitrary cap. On the installed vault
(26,299 transaction rows), one read held the SQL snapshot for 0.250 seconds;
the whole transaction check took 1.350 seconds. The process peak RSS increase
over its imported baseline was 75,308 KiB, including existing scan structures
and artifact checks; this is not an isolated measurement of added memory.
The tradeoff must be revisited if metadata growth makes this transient cost
unacceptable. The operation rows remain streamed and are not copied wholesale.

Regressions verify that a writer can commit during file checks, that an old
snapshot remains coherent when the writer deletes a later operation, and that
the next health read detects that defect. Existing tests beyond the former
10,000-row cap continue to require complete scanning, genuine corruption
detection, quarantine lineage and conservative deletion handling.

The previous SQL-under-filesystem scan is replaced, with no alternative mode
or compatibility branch. Existing quarantines and capture history are retained;
this change does not turn those outstanding findings into a healthy result.
