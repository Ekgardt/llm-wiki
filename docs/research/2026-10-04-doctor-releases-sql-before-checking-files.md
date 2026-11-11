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


## 2026-10-05: bounded reconciliation of terminal changes

The installed strict reader can reject a valid snapshot because a canonical
capture commits while files are inspected. A genuine adopted capture reproduction
completed the queue task and committed its session evidence, while doctor reported
its older SQL snapshot as unreadable. A separate ordinary committed create
reproduced the same issue. Neither reproduction identifies every historical writer.

The candidate keeps SQL-only snapshot transactions and closes every cursor and
connection before filesystem callbacks. A final complete two-table comparison
retains unchanged immutable row objects and captures changed rows. Changed/new
transactions may be reconciled only in existing terminal states. Removed rows,
unknown identities, changed nonterminal states and live writer ownership still
refuse. Every operation is structurally validated; changed positions or hashes
are not ignored. No operation or capture name earns special treatment.

Pure transaction-row contributions belong to this one inspection. Their keys
include complete SQL row values, complete operation positions and current artifact
membership. Every round freshly inventories the filesystem and checks owners,
quarantine and canonical receipt proofs. Counters and deletion codes are rebuilt
from the current contributions. The final exact SQL equality covers both tables,
all inspected fields, order and counts after those proofs. Unknown or incomplete
artifact authority refuses. A changed snapshot goes through that process again
under the original caller deadline; no retry count, sleep, setting or budget is
added. Existing strict standalone comparison remains unchanged.

This narrow terminal-only admission preserves the existing nonterminal safety
guards. It does not promise convergence during continuous writes, a health result
for an active writer, or a deletion permit. Review this restriction only with a
separate proof that nonterminal changes can be inspected without losing ownership,
recovery or deletion protections. Memory and full ROOT timing still require actual
qualification; an isolated green test is not a cost claim.

Primary sources reread on 2026-10-05: [SQLite isolation](https://www.sqlite.org/isolation.html),
[Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html), and
[Microsoft.Data.Sqlite contention](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/database-errors).
Installed runtime is Python 3.12.3 with SQLite 3.45.1; Python 3.10 compatibility
remains required. Holding a SQL read transaction through filesystem work repeats
the proven writer lock. Repeating all materialized rows duplicates memory. Ignoring
new rows or trusting file metadata would weaken authority. Reconciliation keeps
those protections and reuses only copied immutable SQL values.

Previously terminal transaction rows and their complete operation records remain
immutable between the inspected snapshots. A formally valid replacement SHA,
path or applied flag still refuses. The current candidate also refuses concurrent
retention metadata changes: allowing changed `updated_at` or
`artifacts_pruned_at` would require a separate canonical-retention proof. Fresh
checks may read their new stable state normally. This conservative concurrent
restriction preserves the previous drift refusal; it is not a record-count cap.

The row-contribution cache never contains an external file, receipt, source or
claim verdict. Complete operation-format validation reruns for every operation on
every round, independently of cached transaction-row contributions. Existing
strict comparison helpers and their old negative assertions remain unchanged.


The final candidate passed 276 related tests with three unavailable Windows-only
junction cases, including the unchanged strict snapshot guards. All 58 written or
changed functions and nested handlers passed actual Lizard (maximum CCN 4), two-if
and two-level shape checks, Ruff and Python 3.10 grammar. The now-unused private
`_finish_transaction_snapshot` was removed after searching scripts, tests,
integrations, skills, rules, docs and benchmarks. Standalone strict comparison
and its active test consumers remain.

A read-only ROOT transaction-only observation used the explicitly authorized
60-second caller deadline in separate fresh processes. The installed baseline
returned snapshot drift after 10.340 seconds, using 681,204 KiB peak RSS, and
reported 40 unresolved quarantines in its older snapshot. The candidate before
unused-function cleanup performed two complete SQL/filesystem rounds in 14.850
seconds, using 731,388 KiB peak RSS. It returned `read_error=False`, with 39
unresolved quarantines and 145,650 committed transactions. It did not claim a
healthy vault. The snapshots differed by 27 committed transactions; neither speed,
resolution of the missing quarantine finding, nor full nightly health follows
from those observations. No model or mutation was run by that diagnostic.

The first copied SQL snapshot and per-row contribution cache remain material RAM
costs. Fresh filesystem passes still consume caller time and may leave insufficient
time for later queue checks. Continuous changes, unsafe artifacts, a current live
writer or nonterminal drift may still refuse. These costs and safety restrictions
must not be masked by increasing the production budget. A proposed synthetic
large-row benchmark was not created while disk space was unsafe. An owned
navigation-cache retirement refused before deletion because same-user process FD
visibility was unavailable; no ROOT cache or runtime data was deleted.

## Shared daily-source reader capacity — 2026-10-05

Doctor's two current-day supersession reads used an independent 4 MiB bound,
although compilation, archive payloads and evidence resolution support the existing
16 MiB daily source family. A genuine isolated 4.205 MB day, partitioned normally
and published through real temporary Markdown transactions with committed v4 part
receipts, reproduced six read refusals; the above-16-MiB negative already passed.
The candidate imports `evidence_resolver.MAX_DAILY_BYTES` for both reads, removes
the unused independent constant, and keeps the same stable byte reader, exact
source digest, all-part context authority and canonical committed checks.

The seven controls pass, including missing receipt, foreign day, changed original,
wrong whole digest and above-existing-capacity refusal. This is reader compatibility,
not a newly justified optimum for the existing 16 MiB bound. No new setting, format,
archive fallback, historical v3 authority or model call is introduced. A v3 digest
may bind selected part bytes; it cannot be promoted to the current whole-file hash
or accepted through matching prefixes. A read-only review of the 39 dated retained
attempts found all their current days above the old 4 MiB bound, but none of their
saved sizes or digests equal the current whole day. Removing the read mismatch
therefore does not prove those attempts resolved.

Research checked on 2026-10-05: [Python 3.10 pathlib](https://docs.python.org/3.10/library/pathlib.html)
for filesystem identity versus path operations; [SQLite isolation](https://www.sqlite.org/isolation.html)
for canonical transaction observations; [OWASP resource exhaustion](https://community.owasp.org/attacks/Denial_of_Service)
for preserving finite resource admission. Unbounded reads and a separate new
threshold were rejected. The CLI doctor default remains 5 seconds; nightly health
uses its existing 60-second caller budget. No whole-health or audit closure follows.
