# An archive race shares its wait budget

Researched 2026-09-29. Windows Python 3.10, job 109412398761 in run
36570286560, failed `test_archive_winning_finalization_race_deletes_before_failure_records`.
The source-failure row was absent. The primary traceback in the thread warning
shows `sqlite3.OperationalError: database is locked` from `BEGIN IMMEDIATE`.
This was not an evidence-hash or compiler-part regression.

The fixture deliberately holds the archive's queue finalization transaction while
another thread attempts a write. Thread/event joins use the common `LONG_TIMEOUT`
hang bound, but the queue connection still used the production 5-second busy
timeout. Real archive deletion, including Windows ACL operations and durable
filesystem writes, completes before the transaction releases. Thus the writer can
exhaust its shorter SQLite wait even though both threads finish within the test's
declared bound. The test did not capture that worker exception, so it surfaced as
a warning plus the later missing-row assertion.

## Research and choice

- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html) documents
  that the busy handler stops waiting after its configured interval and returns
  SQLITE_BUSY. Waiting for a thread longer does not extend the database wait.
- [CPython test support](https://docs.python.org/3/library/test.html#test.support.LONG_TIMEOUT)
  distinguishes a hang bound suitable for slow buildbots from a performance limit.
  The project already implements this policy in `tests/slow_machine.py`.
- [pytest failure handling](https://docs.pytest.org/en/stable/how-to/failures.html)
  explains unhandled thread exceptions and their warning category. Make that
  category an error for these archive tests so a worker cannot fail silently.

Fetched all three independent primary sources on the research date. Use the
existing configurable test hang bound for queue waits in the archive fixture.
Do not increase production timeouts, sleep/retry around the failure, skip Windows,
or accept a missing failure record. The serialization and retained-evidence
assertions are unchanged. No archive test in this fixture asserts expiry of the
production queue busy timeout; that value remains covered by queue contract tests.

Tradeoff: a genuinely deadlocked fixture may wait until the existing hang bound.
The fixture restores the original defaults automatically through monkeypatch.
There is no new production dependency, limit, fallback, or legacy implementation.
The original failing Windows race must pass again before platform qualification
is claimed; a Linux pass alone does not establish that.

## Related plugin harness

Windows Python 3.13 job 109412398785 failed the OpenCode session-start context
test in `subprocess.communicate` after its literal 30-second timeout. The harness
uses immediate mocked responses, and the plugin clears its timers in `finally`.
The same six tests pass locally in 0.25 seconds; other matrix versions passed.
The available log does not establish why this Node process failed to finish on
that runner, so a slow runner is a hypothesis, not a proven root cause.

The harness nevertheless contradicts the existing test policy: it expects
successful real subprocess work but uses an unscaled literal hang bound. Use
the same `LONG_TIMEOUT` there. The exact output and exit-code assertions stay
unchanged. A scan of all OpenCode test modules found no other literal timeout.
Repeat Windows CI is required; this change alone does not establish resolution
of the observed runner delay.
