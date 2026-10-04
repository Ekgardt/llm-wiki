# Doctor releases SQL before checking files

Research and qualification date: 2026-10-04. Installed Python 3.12.3 and
SQLite 3.45.1; minimum supported Python 3.10.

A transaction health check held an explicit read transaction while traversing
artifact directories, reading files and validating receipt evidence. A genuine
adopted-runtime reproduction showed that the canonical owner's heartbeat could
not commit during that filesystem callback. This establishes a mechanism; it
does not identify the exact historical lock holder or establish the cause of a
host's five-second hook timeout.

The repair captures both complete SQL tables in one short consistent snapshot,
closes cursors and the connection, and inspects files afterward. Before accepting
a verdict, it compares every transaction field and every operation field, row
order and count against the captured snapshot in another SQL-only transaction.
That final comparison streams the existing batch size instead of making a second
complete copy. It runs after quarantine and filesystem proofs. A changed snapshot
or expired caller deadline produces an unreadable result, never health or a
deletion permit. Deadline expiry at snapshot commit is checked too.

Standalone receipt readers reuse the same capture and revalidation mechanism.
Their canonical receipt checks use an autocommit connection between those short
snapshots. Existing source, receipt and committed-operation integrity guards stay
in force. Retained history, live owners and undo artifacts still block deletion.
No runtime directory, database, WAL mode, daemon, setting or retry was added.

Fresh primary sources:

- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html) explains why
  a live shared read lock prevents a writer from acquiring exclusive access.
- [Python sqlite3](https://docs.python.org/3.12/library/sqlite3.html) distinguishes
  a transaction context manager from closing its connection. Explicit cursor and
  connection cleanup is necessary here.
- [Microsoft.Data.Sqlite errors](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/database-errors)
  documents contention and timed retries from another SQLite binding. Increasing
  a waiting period does not eliminate the long-lived reader.

Keeping the original reader rejected the reproduced writer. Increasing waiting
times would extend latency. WAL and another operational database violate the
existing runtime contract. A second complete materialized snapshot reached about
1 GiB peak RSS on the actual offline specimen. Streaming the final comparison
preserves the same consistency proof with less retained data.

The first snapshot remains a material memory cost: on an online copy containing
128,388 transactions and 146,063 operations, its RSS increase was 508,348 KiB.
The subsequent streamed comparison increased RSS by 18,692 KiB. The measured SQL
passes took 4.402 and 2.048 seconds; peak RSS was 601,120 KiB. These measurements
exclude the complete filesystem check, have uncontrolled cache and competing-load
conditions, and establish neither a general speedup nor a five-second guarantee.
All transaction metadata remains necessary for recovery, lineage, capture fences
and detecting a changed authority; dropping it to improve a benchmark would weaken
the check.

The frozen repair passed 375 related tests, with three Windows-junction tests
unavailable on Linux. Changed callables, including nested functions and guards,
passed actual complexity and lint checks. Twelve guards passed again after
installation against identical isolated source. The installed full health check
completed all 22 sections, but returned an unreadable transaction state, retained
queue work and a corpus traversal refusal. This is not evidence of complete system
health, historical recovery, full nightly operation or repaired host hooks.

Qualification artifacts and the original reproduction remain private local logs;
the public repository ships neither the database specimen nor private knowledge.
