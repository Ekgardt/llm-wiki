# Doctor knows the abort states

Date: 2026-09-26. Audit 2026-09-26 C-12 (two of its parts).

## Facts

- The coordinator v3 schema allows `aborting` and `aborted`
  (`markdown_transaction.py`, the `state IN (…, 'aborting','aborted')` check);
  `abort_for_discard` writes them and `_recover_aborting` settles them.
  `doctor.TRANSACTION_STATES` did not list them, so doctor called such a row "a
  transaction in a state this runtime does not define", and an `aborting` row was
  not counted as unsettled.
- `compile_memory` writes `logs/compile-drops-<day>.jsonl` once per day;
  `maintenance_helpers.MAINTENANCE_REPORT_PATTERNS` did not include it, so the
  family grew without bound.
- SQLite `CHECK` constraints (https://www.sqlite.org/lang_createtable.html,
  fetched 2026-09-26): "Each time a new row is inserted into the table or an
  existing row is updated, the expression associated with each CHECK constraint is
  evaluated … If the result is zero … then a constraint violation has occurred." The
  schema's check defines the states a row may hold; a reader has to know the same
  set.

## Decision

- Doctor lists `aborting` (unsettled, with preparing/prepared/applying, one
  `UNSETTLED_TRANSACTION_STATES` tuple used everywhere) and `aborted` (terminal).
- `compile-drops-*.jsonl` joins the maintenance report families under the same
  age, count and size retention.
- Not done here from C-12: the maintenance lock judged by PID alone, lease times
  compared as text, `pending/` left unfinished in intent adoption, and `prune()`
  holding the writer gate without a deadline stay open.

## Files

- `scripts/doctor.py`
- `scripts/maintenance_helpers.py`
- `tests/test_doctor_knows_the_abort_states.py`
- `CHANGELOG.md`
