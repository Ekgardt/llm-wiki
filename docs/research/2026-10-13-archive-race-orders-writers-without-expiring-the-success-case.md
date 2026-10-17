# Archive finalization race: separate successful serialization from lock refusal

Research and qualification date: 2026-10-13.

CI run 38320039403, Windows Python 3.13 shard 1, failed the archive-winning
race. The failure writer raised `database is locked` at `BEGIN IMMEDIATE`;
archive deletion succeeded, but the expected failure record was absent.
The test took 16.88 seconds on that runner. The production queue connection
has a five-second busy timeout. The test deliberately holds the finalization
lock before releasing deletion, and then waits for a successful concurrent
write. It therefore mixed a successful-serialization scenario with a separate
lock-refusal policy. This is not evidence that the new receipt projection
caused the failure or that the live capture failure has the same cause.

The original test passes locally with the default budget. Setting only the
observed queue busy budget to 100 ms reproduces the same absent-record
assertion and the same unhandled writer exception, while the first writer
still holds its deliberate 250 ms observation window. After correction the
same reduced-budget observation passes. This reduction is a diagnostic
experiment, not a product configuration change.

The successful writer in this one race fixture now receives the existing
`LONG_TIMEOUT` hang bound when it opens its connection. All production code,
default busy budgets, the deliberate observation window, finalization fence,
archive deletion assertions, and durable failure-record assertion remain.
Writer exceptions are collected and asserted, and its successful completion
is explicitly required. The existing real exclusive-lock test in
`test_secondary_receipt_consumers_keep_authority.py` still requires refusal
at a 50 ms caller deadline, no mutation, and elapsed time below one second.
This change does not qualify or repair the live capture loss.

Alternatives rejected: rerunning until green would not remove the fixture's
conflicting timing assumptions; increasing the production default would
change runtime behavior without measurements; suppressing the exception or
removing the durable-record assertion would conceal failed work. The chosen
fixture change preserves the success scenario and the separate refusal guard.

Sources consulted directly:
- SQLite busy-timeout contract: https://www.sqlite.org/c3ref/busy_timeout.html
- CPython 3.13 connection timeout and context-manager semantics:
  https://docs.python.org/3.13/library/sqlite3.html
- pytest guidance on uncontrolled state and thread failures:
  https://docs.pytest.org/en/stable/explanation/flaky.html

Full Windows matrix qualification is required before installation.
