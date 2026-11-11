# A full repair scenario requests a full check

Date: 2026-10-13. Scope: the legacy JSON-queue refusal scenario.

CI38336899409 Windows Python3.12 shard3 expected error plus two retained
legacy records, but observed degraded plus zero. The scenario called the whole
repairing doctor without an explicit budget, thus inheriting its five-second
quick-health default. Artifact scanning returns incomplete state when that
budget expires; repair can be deferred, so that result cannot establish the
scenario's required full observation.

A causal regression runs the original complete scenario with its real
filesystem probe, then advances only doctor's logical clock by twice the
quick-health default. Original assertions fail with two retained files
reported as zero; the error/degraded distinction differs by expiry point.
This is simulated unrelated probe duration, not a performance measurement,
not a virtual replacement for any actual host-latency guard, and not proof
of the exclusive Windows delay location. An initial keyword-only observation
was retained as an unshipped diagnostic; the final regression observes the
original behavior instead of asserting an implementation keyword.

The fixture now passes the existing `LONG_TIMEOUT` completion budget, as
required for a deadline the test expects to remain usable on the slowest
supported machine. The product's quick default and all actual refusal
requirements remain. Neither record may be imported or removed; no migration
marker may appear; the report must contain the unsupported-queue error.
The same regression then completes after the longer unrelated probe.
No production code, ACL rule or runtime limit is changed. Actual Windows
qualification remains required in the next full matrix.

Alternatives rejected: weakening the expected count/status would conceal an
incomplete observation; changing the product default would alter quick-health
behavior; sleeping to reproduce machine latency would be nondeterministic;
subtraction of elapsed phases would hide failed checks. An explicit existing
completion budget separates this behavioral scenario from budget-exhaustion
tests, which retain their own short deadlines.

Primary sources already consulted for this fixture boundary:
- CPython thread/event timing and independent execution:
  https://docs.python.org/3/library/threading.html#thread-objects
- pytest uncontrolled-state and timing guidance:
  https://docs.pytest.org/en/stable/explanation/flaky.html
- Microsoft thread creation and scheduling:
  https://learn.microsoft.com/en-us/windows/win32/procthread/creating-threads

The project policy and its measured slow-machine basis are stated in
`tests/slow_machine.py`; this introduces no new numeric limit.
