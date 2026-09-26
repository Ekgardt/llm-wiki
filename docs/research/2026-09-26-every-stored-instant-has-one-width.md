# Every stored instant has one width

Date: 2026-09-26. Audit 2026-09-26, finding C-12 (lease times compared as text).

## What was wrong

The coordinator database (`run/markdown-transactions-v3.sqlite3`) compares times in
SQL as text: fence and owner expiry (`expires_at > ?`), project leases, the history
prune cutoff (`updated_at < ?`). The writers in `markdown_transaction.py`,
`project_journal.py` and `repair_orphaned_checkpoint_names.py` used
`isoformat().replace("+00:00", "Z")`, which drops the fraction on a whole second.
`Z` (0x5A) sorts after `.` (0x2E), so `10:00:00Z` compared as later than
`10:00:00.500000Z`: a lease written on a whole second read as live half a second
past its expiry. `operational_ownership.py` (research note of 2026-09-17) and `blackboard.py`
already write six fraction digits; the coordinator and journal writers did not.

## Decision

One writer, `iso_time.utc_text`: UTC, `timespec="microseconds"`, `Z`. Every
stored instant these three modules write goes through it. Readers already accept
both shapes (`fromisoformat` after `Z` → `+00:00`), so rows written before the fix
stay readable; leases and fences written earlier expire within seconds and are
rewritten in the new width.

## Source

Python documentation, `datetime.isoformat`, fetched 2026-09-26 from
https://docs.python.org/3/library/datetime.html:

- "`YYYY-MM-DDTHH:MM:SS.ffffff`, if `microsecond` is not 0"
- "`YYYY-MM-DDTHH:MM:SS`, if `microsecond` is 0"
- "`'microseconds'`: Include full time in `HH:MM:SS.ffffff` format."
- Example: `my_datetime.isoformat(timespec='microseconds')` →
  `'2015-01-01T12:30:59.000000'`.

Fact from the page: the default output's width depends on the value; the
`microseconds` timespec fixes it. Conclusion (mine): only a fixed-width text keeps
text order equal to time order.

## Files

- `scripts/iso_time.py`
- `scripts/markdown_transaction.py`
- `scripts/project_journal.py`
- `scripts/repair_orphaned_checkpoint_names.py`
- `tests/test_every_stored_instant_has_one_width.py`

## Against recurrence

`tests/test_every_stored_instant_has_one_width.py` finds every script that compares
a `*_at` column with a bound parameter in SQL and fails when that script calls
`isoformat()` without `timespec` on anything but a calendar date. A new module that
starts comparing times in SQL is covered without being listed.
