# Doctor reads the open rows first

Date: 2026-09-26. Audit 2026-09-26 item A-3 (and L10 of the state audit).

## Fact
- `doctor._bounded_operational_rows` runs `_TRANSACTION_QUERY + " LIMIT ?"` with
  no `ORDER BY`. On the live vault (23 640 rows, read-only 2026-09-26) the 10 000
  rows it judged were those created 2026-08-26 to 08-30; the 13 640 newer rows,
  including every row in the undo window, were never judged. A transaction that
  conflicts or sticks today is reported "healthy within the scanned rows".
- The operation scan has the same shape and is truncated too; the queue scan
  (`doctor.py` around 1928) likewise.

## Source (fetched 2026-09-26)
SQLite, "SELECT", section 4 "The ORDER BY clause",
https://www.sqlite.org/lang_select.html: "If a SELECT statement that returns more
than one row does not have an ORDER BY clause, the order in which the rows are
returned is undefined." A bound on an unordered scan decides nothing about which
rows it keeps.

## Decision
- Transactions are read open states first (anything neither `committed` nor
  `discarded`), then newest first (`rowid DESC`). The bound then drops only the
  oldest settled history, which is exactly what a verdict does not need.
- Operations are read for those same transactions, newest first.
- The queue scan orders its unfinished tasks first, then newest.

## Files
- scripts/doctor.py
- tests/test_doctor_reads_the_open_rows_first.py
