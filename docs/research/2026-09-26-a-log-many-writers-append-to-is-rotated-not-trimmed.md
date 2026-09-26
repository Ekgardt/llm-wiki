# A log many writers append to is rotated, not trimmed

Date: 2026-09-26. Audit 2026-09-26, finding C-12 (hook-errors trim race).

## What was wrong (facts, read in the code)

`logs/hook-errors.log` was bounded (since 2026-09-24) by the same in-place trim as the
scheduler logs: read the last 2 MB, write them at offset 0, truncate. The scheduler logs
have one writer, the pass's own redirected output. `hook-errors.log` has four
(`integration_adapter._log_checkpoint_error`, `memory_state._keep_corrupt_copy`,
`session_end_project_tag._safe_write_error`, `session_start_project_state._safe_write_error`),
each an independent hook process that opens, appends and closes. A line appended
between the trim's read and its truncate is cut off.

## Source

logrotate(8), Linux man-pages, fetched 2026-09-26 from
https://man7.org/linux/man-pages/man8/logrotate.8.html, option `copytruncate`:
"It can be used when some program cannot be told to close its logfile and thus might
continue writing (appending) to the previous log file forever. Note that there is a very
small time slice between copying the file and truncating it, so some logging data might
be lost."

Conclusion (mine): copy-and-truncate is for a writer that keeps its file open — our
scheduler logs. Writers that reopen the file for every line are served by moving the
file aside, which loses nothing.

## Decision

- `maintenance_helpers.ROTATED_LOG_NAMES` (`hook-errors.log`): past 2 MB the file is
  renamed to `hook-errors.log.1`, replacing the previous generation; the next hook
  creates a fresh file, and a hook that had the old one open finishes its line there.
  At most two generations, about 4 MB.
- Doctor's hook check reads the rotated generation and the live one, so a burst of
  failures just rotated away is still reported.
- The scheduler logs keep the in-place trim; their only writer is the pass itself.

## Against recurrence

`tests/test_a_log_many_writers_append_to_is_rotated_not_trimmed.py` finds every `*.log`
name a script opens for append and requires it to be rotated and never trimmed in place.

## Files

- `scripts/maintenance_helpers.py`
- `scripts/doctor.py`
- `tests/test_a_log_many_writers_append_to_is_rotated_not_trimmed.py`
- `tests/test_every_store_has_a_bound.py`
