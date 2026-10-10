# Projector contention is not a retry count

Research date: 2026-10-08. CI run 37815535023, Linux Python 3.10 shard 3,
failed `test_simultaneous_projectors_append_once_per_event`. Its worker stopped
after 100 legal contention responses and reported that the lease never became
available. The other 2,835 tests passed; 28 were skipped. This was not evidence
that a lease remains unavailable forever.

The test now uses the project's existing monotonic `LONG_TIMEOUT` hang bound.
It still starts both projectors together and verifies exactly one journal
record per event, sequences 1 and 2, and their actual durable representations.
Only the test's expectation changed; production lease handling did not change.
A causal regression supplies 100 legal losses followed by real checkpoint
calls. All three allowed exception types failed on the old counter and pass
with the deadline. No sleeps, exception suppression or weaker postconditions
were added. The journal and timeout-policy set passed 108 tests.

Alternatives: increasing the counter would keep the hardware-dependent
assumption; serializing the writers would remove the race under test; changing
production lease handling is unjustified by this failure. The existing hang
bound is calibrated for supported slow CI machines and detects a stuck test.
It is not a performance target or a product retry limit.

Independent primary sources checked on the research date:

- [CPython threading](https://docs.python.org/3.10/library/threading.html):
  synchronization primitives and scheduling of concurrent I/O work.
- [SQLite locking and concurrency](https://www.sqlite.org/lockingv3.html):
  competing readers and writers and the rollback-journal locking protocol.
- [pytest flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html):
  concurrent state and overly strict timing assumptions in test failures.

The overall matrix and audit are still separate qualification requirements.
