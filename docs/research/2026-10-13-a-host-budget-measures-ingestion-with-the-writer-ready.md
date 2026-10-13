# A host budget measures ingestion with the competing writer already ready

Date: 2026-10-13. Scope: the foreground capture contention fixture.

CI38329687452 failed Windows Python3.12 user-prompt capture at9.219s and
Python3.14 post-tool capture at6.1705s against the unchanged five-second host
budget. Recorded ACL costs were2.925s and0.1984s respectively. The latter
rules out ACL commands alone as a sufficient explanation. These failed checks
remain failed; nothing is subtracted from their results.

The scenario's mocked wake callback started a competing writer and waited for
its writer gate inside `adapter.ingest_event`. Production wake spawns its
worker without waiting for that worker's gate. The host stopwatch therefore
included synchronization that the fixture introduced for another actor.
A new event-order regression runs the complete original scenario and verifies
that the competing writer was ready at ingestion entry. Both original cases
fail this regression. It uses real events and the real clock: no artificial
sleep, virtual stopwatch, timeout subtraction or runtime guard replacement.

The corrected fixture establishes the competing writer before starting the
host stopwatch. This tests the stronger busy-from-entry condition. Its
existing readiness/join hang bounds still apply. The original five-second
assertion, complete durable capture, ready file existence, subsequent worker
delivery, drained checkpoint and actual project journal all remain. Cleanup
still releases and joins the competing writer even if ingestion fails.
The Windows observer records the unchanged actual ingestion call and its
real ACL commands. No production code, ACL enforcement or timeout is changed.

The causal regression establishes the wrong measurement boundary; it does
not assign every excess millisecond in the failed Windows runs to that
boundary. Full Windows CI must confirm the corrected actual host scenario.
If it still exceeds five seconds, its remaining production cost is unresolved.

Rejected alternatives: raising the host budget would weaken its contract;
subtracting measured setup from an already failed result would hide failure;
suppressing the durable checkpoint guard would weaken reliability; replacing
ACL enforcement without evidence would change security while leaving the
unexplained non-ACL cost. Preparing the actor through its readiness event is
the direct synchronization fix, with no arbitrary delay.

Primary sources consulted:
- CPython thread start and event/join semantics:
  https://docs.python.org/3/library/threading.html#thread-objects
- pytest on uncontrolled state and threaded test setup:
  https://docs.pytest.org/en/stable/explanation/flaky.html
- Microsoft on thread creation and independently scheduled execution:
  https://learn.microsoft.com/en-us/windows/win32/procthread/creating-threads

Python3.10-compatible test syntax is retained. No dependency, runtime root,
provider contract or install definition changes.
