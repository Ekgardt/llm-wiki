# Daily-log selection

scripts/llm_wiki/daily_log_selection.py owns the existing DAILY_LOG_NAME pattern,
daily_logs and closed_daily_logs. memory_state reexports the same three objects
for compatibility; every existing consumer can keep its imports. No implementation
copy or wrapper remains in memory_state.

The selector takes an explicit directory. Missing/non-directory roots return an
empty list. It globs immediate *.md entries, applies the existing full filename
pattern and sorts paths. The closed-day view removes the newest matching entry,
regardless of today's clock. Empty and single-day views therefore have no closed
entries. Selection never reads file bodies or records processing receipts.

This is filename-shape selection: Unicode digits and calendar-invalid numeric
names retain the regular expression's existing semantics. Date-shaped directories
are not newly excluded; Path.glob and is_dir preserve their platform/filesystem
behavior. This is neither calendar validation nor a hostile-filesystem boundary.
No current time, environment root, operational state or compile trigger is read.

## Qualification — 2026-11-03

The approved autonomous plan permits the internal split. Selection is shared by
compile, pending-work, lint, co-activation, fact keys and MCP readers; storing it
with runtime coordination adds unrelated initialization. Duplicating the rule in
consumers risks drift. Adding calendar validation or changing open-day scheduling
would alter behavior and is outside this transfer.

Research checked [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[dependency contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
and [Python 3.14 paths](https://docs.python.org/3.14/library/pathlib.html).
Concrete filesystem selection remains explicit and separately testable.

Both function ASTs and the pattern remain unchanged. Existing integration tests
retain their imports and assertions, including real committed compile receipts.
The owner adds actual directory-selection cases and fresh import isolation; the
consumer checks public identity. Complete historical filename/open-day records
remain packet context. Exact byte reconstruction and the 100000-token gate apply;
these checks do not claim paired full-cycle model savings or live installation.


## Preserved structure reference — 2026-11-03

The supplied-directory selector has no runtime-state dependency; memory_state
retains public object reexports and all operational state ownership.
