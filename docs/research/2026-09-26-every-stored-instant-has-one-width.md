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

## 2026-10-05: adapter JSON clocks retain explicit precision

The regression scanner also found three bare serializers in integration_adapter:
pending occurrence time, in-flight checkpoint time and capture occurrence time.
The module's actual SQL expiry comparison already binds iso_time.utc_text.
These three values instead travel through JSON and readers using
`datetime.fromisoformat`; a SQL lexical-order defect in those paths was not
established. Their old whole-second strings nevertheless omitted fractional
precision while nonzero fractions used six digits.

The three serializers now specify `timespec="microseconds"` while retaining the
existing offset spelling, including `+00:00`. They do not switch JSON clocks to
`Z`, change the instant, rewrite retained intents, change event IDs or relax the
SQL regression scanner. Historical strings with and without fractions still
round-trip through the existing readers. New controls exercise the real checkpoint
reducer, in-flight replay and late-session filing, plus preservation of an
existing non-UTC offset. No mixed-offset lexical-order claim is made.

Primary sources freshly checked on 2026-10-05:

- [Python 3.10.22 datetime documentation](https://docs.python.org/3.10/library/datetime.html):
  explicit microseconds retain supported precision and offsets; fromisoformat
  reads the forms emitted by isoformat.
- [RFC 3339 section 5.1](https://www.rfc-editor.org/info/rfc3339/): lexical sorting
  requires the same offset representation and fractional width.
- [SQLite datatype and collation documentation](https://www.sqlite.org/datatype3.html):
  BINARY text comparison uses memcmp, distinct from parsing JSON timestamps.

Excluding the adapter from the scanner was rejected because it could conceal a
future SQL writer drift. Converting its JSON clocks to the common Z writer was
unnecessary and would alter offset representation. Explicit precision at the
existing serializers is the compatible change; no schema, path, runtime, setting,
dependency or provider contract changes. Original controls recorded five failures
and eight passes. After correction the ten related modules passed 101 tests in
14.86 seconds. Actual Lizard/AST analysis accepted all twelve changed/new callables
(maximum CCN 5, one if statement and branch/loop depth two), and Ruff passed.
Nothing is installed by this candidate qualification.
