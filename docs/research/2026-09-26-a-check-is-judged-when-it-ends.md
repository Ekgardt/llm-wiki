# A check is judged when it ends

Date: 2026-09-26. Audit 2026-09-26 item B-19 (a regression of the A-9 fix).

## Fact
- A-9 turned a check's `error` with a `read_error` into "not completed" when the
  doctor's deadline had passed (`doctor._unfinished_when_late`).
- `_collect_checks` applied it to every check after all of them had run, so the
  deadline was tested at the end of the whole run. A check that failed early and
  genuinely (a corrupt `run/queue.sqlite3`) was reported as "not completed because
  the time budget was exhausted" whenever a later check (the LSP check keeps its
  own budget) pushed the run past the deadline. Reproduced by the audit: budget
  30 s → `error`, budget 0.05 s → `degraded`.

## Source (fetched 2026-09-26)
Python documentation, `time.monotonic`, https://docs.python.org/3/library/time.html:
"Return the value (in fractional seconds) of a monotonic clock, i.e. a clock that
cannot go backwards." Whether a failure happened after the deadline is a fact
about the moment that check ended; reading the clock later answers a different
question.

## Decision
Each check is judged the moment it returns: `_collect_checks` runs them one by one
and applies `_unfinished_when_late` to each result immediately.

## Files
- scripts/doctor.py
- tests/test_a_check_is_judged_when_it_ends.py
