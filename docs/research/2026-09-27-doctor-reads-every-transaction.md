# Doctor reads every transaction

Date: 2026-09-27. Scope: `scripts/doctor.py`, the transaction health check (law 7, law 9).

## Finding

`doctor.MAX_OPERATIONAL_ROWS = 10_000` capped the transaction and operation scans.
The installed vault holds 29 275 transactions and 37 509 operations (read-only count,
2026-09-27), so every report carried `transaction_scan_truncated` and
`transaction_operation_scan_truncated`, judged a third of the rows, and abstained from
the operation check entirely. The quarantine count (`quarantined_unresolved`) was not
affected: it is computed by `transaction_lineage` over whole tables.

Measured on a copy of the live database (python `sqlite3` backup API, never the CLI,
never the live file):

| code | rows judged | time | Python peak | verdict codes |
|---|---|---|---|---|
| HEAD 986895e9 | 10 000 of 29 369 | 0.52 s | 22.6 MB | `…metadata_corrupt`, both `*_truncated` |
| this change | 29 369 of 29 369 | 0.63 s | 14.4 MB | `…metadata_corrupt` only |

The capped scan was not buying anything: the whole scan costs 0.11 s more.

Two more defects of the same family (a verdict about the bound, not the rows) showed up
while testing:

- **The capped scan accused a healthy vault.** A synthetic vault of 10 200 healthy
  transactions with one operation each read as `transaction_metadata_corrupt` on HEAD.
- **The undo-directory listing has its own cap** (`MAX_RUNTIME_ENTRIES = 10_000`). A
  row whose `run/transactions/<id>` lay past the listing looked like a row whose
  directory is missing, and was called corrupt. The installed vault held 5 547 entries
  in the morning and 5 990 in the afternoon of 2026-09-27.
- **Quarantined before planning.** `_commit_promotion` writes the plan hash and the
  operations in one database transaction. When binding the project reservation
  refuses, that transaction rolls back and `_quarantine_failed_promotion` sets the row
  `quarantined` with `plan_hash = ''` and no operations. Doctor exempted only
  `preparing` and `discarded`, so these rows were corrupt metadata. There are 9 on the
  installed vault, and with the live undo directory they were the only rows
  `transaction_metadata_corrupt` rested on: that finding is what the session-start
  health line reported as "a transaction in a state this runtime does not define".

## Sources

- SQLite, Isolation In SQLite: "In rollback mode, SQLite implements isolation by
  locking the database file and preventing any reads by other database connections
  while each write transaction is underway. … before any changes are made to the
  database file on disk, all readers must be (temporarily) expelled in order to give
  the writer exclusive access to the database file." https://www.sqlite.org/isolation.html
- SQLite, Transaction: "Automatically started transactions are committed when the last
  SQL statement finishes." and "If the first statement after BEGIN DEFERRED is a
  SELECT, then a read transaction is started." https://www.sqlite.org/lang_transaction.html
- Python, sqlite3: `fetchmany` "Return the next set of rows of a query result as a
  list. Return an empty list if no more rows are available."; with `isolation_level`
  set to `None`, "transactions are never implicitly opened."
  https://docs.python.org/3/library/sqlite3.html

## Decision

- Both tables are streamed whole in batches of `SCAN_BATCH_ROWS = 1 000`, so memory
  holds one batch and the known ids and operation positions, not the tables; the
  deadline is checked between batches.
- The id, operation and transaction reads run in one read transaction (`BEGIN` …
  `COMMIT`). The read connection autocommits, so each statement used to see its own
  state, and a transaction committed between the reads looked like a row without
  operations or an operation of an unknown row. In rollback-journal mode the read
  transaction holds writers off for the scan (0.63 s on the installed vault), within
  their busy timeout.
- The operation read has no join: an operation whose transaction is gone reaches the
  identity check instead of vanishing.
- An incomplete undo listing abstains from the per-row directory comparison; the
  deletion refusal (`transaction_artifact_state_unknown`) stays.
- A row quarantined straight out of `preparing` (empty plan hash, no operations) is
  valid; a quarantined row with a plan still needs its operations.
- Removed with the cap (law 8): the `*_scan_truncated` codes, `transaction_scan_incomplete`,
  the "lower bound" verdict, the separate whole-table state count, and their tests.
  `MAX_OPERATIONAL_ROWS` stays for the small owner, lease and queue tables.

Alternatives rejected: raise the cap (the next vault outgrows it again; law 9 wants a
basis, and the measured cost says none is needed); answer every check with SQL
aggregates (the row checks parse JSON and compare to the filesystem, so they need the
rows; streaming gives the same bounded memory without a second implementation).

## Guard

`tests/test_doctor_bounded_scan_truth.py`: vaults past the old cap are read whole and
clean; a defect in the oldest row, a missing operation past the old operation cap and
an orphan operation are found; a quarantine before planning is not corrupt and a
planned one without operations is; an undo listing past its bound refuses deletion
without accusing. Six of its fifteen tests fail on HEAD.

## Not verified here

On the live undo directory, 70 entries named no transaction row at the time of the
check. Some may be skew between the database copy and the later listing; whether
committed rows are dropped from history while their directories stay is not checked.

## The same class in the installed-vault check (2026-09-27, later)

`installed_memory_repair._bounded_rows` read every operational table it validates
(queue tasks, capture intents, owner and fence tables, the unpruned transaction rows,
blackboard claims) with `LIMIT 10 001` and raised `ValueError` past 10 000 rows; the
callers turn that into `transaction_state_unreadable`, which refuses a backup and the
`run/` deletion check — the failure A-11 already met once at 23 664 rows.

Measured on a backup-API copy of the installed databases at 14:25 UTC: the transaction
query (rows not committed, or committed with images still held) returned 6 095 rows —
the two-day undo window; the largest queue table held 347 rows. So the installed vault
does not refuse today, and the claim that it does was not reproduced. The window's
size is activity, not a constant: the busiest two consecutive days on record hold
6 834 transactions, 68 % of the cap, and more agents raise it. A cap tied to a count
that grows with use is the defect doctor had; it gets the same fix.

- Rows are streamed in `SCAN_BATCH_ROWS` batches through one helper in
  `reliable_memory` (`streamed_rows`), which doctor now uses too, instead of each
  module holding a copy (law 6). The deadline is checked between batches.
- A table that only has to be empty is asked `SELECT 1 … LIMIT 1`; reading all its
  rows to learn that one exists was the cap's only use there.
- `_MAX_OPERATIONAL_ROWS` has no reader left and goes (law 8); `doctor.MAX_OPERATIONAL_ROWS`
  still bounds its own small owner and lease tables.

### The 70 undo directories without a row

Re-listed on the installed vault and compared with a database snapshot taken after the
listing, so no row created before the listing can be missing: 6 068 directories, one
without a row. It is 25 minutes old, its `before/` and `after/` are empty and the pid in
its `owner.json` is gone — a prepare that failed before inserting its row. The existing
nightly `_remove_unnamed_artifact_roots` retires such a root once it is an hour old.
The earlier count of 70 came from listing after the copy, which the doctor note above
already suspected; nothing is lost and nothing needs a new mechanism.

## Runtime directory growth (2026-09-30 follow-up)

Installed queue-results exceeded 10,000 entries. Doctor counted exactly 10,000
and reported artifact_truncated; installed runtime inspection used the same
unmeasured count ceiling and called the state unreadable. Retained successful
captures are legitimate evidence, not files to delete to satisfy a health limit.

Graph and source inspection followed capture terminal/decision publication into
queue-results, doctor counting and transaction artifact comparison, and installed
runtime deletion checks. Both checks already carry monotonic deadlines. Choose
whole listings under those existing deadlines, preserving containment/type checks
and explicit incomplete results. No new setting, storage location, or runtime
contract is introduced. Raising the count ceiling merely moves the same failure;
deleting valid evidence weakens retention. The archive's separate bag-selection
policy is not changed by this runtime-directory repair.

Primary sources checked 2026-09-30:
- Python os.scandir documentation: iterator enumeration, arbitrary order and
  unspecified visibility of concurrent changes; do not infer absence from a
  prefix. https://docs.python.org/3/library/os.html#os.scandir
- Linux readdir manual: stream traversal returns successive entries until the end;
  ordering is filesystem-dependent. https://man7.org/linux/man-pages/man3/readdir.3.html
- Google SRE, Handling Overload: propagate deadlines and stop work once its
  resource budget expires. https://sre.google/sre-book/handling-overload/

The retained identifier set is necessary for ledger/filesystem reconciliation;
queue counting needs no independent record cap. An expired deadline, unsafe entry,
or I/O error remains a refusal, never a successful partial scan. Tests cross the
old 10,000 boundary and retain deadline and path-safety failures.
