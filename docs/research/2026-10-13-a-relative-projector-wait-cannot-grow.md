# A relative projector wait cannot grow

Research and qualification date: 2026-10-13.

Full CI 38420583707 rejected the concurrent-caller wait assertion on Windows
Python 3.11. The log recorded neither the observed wait values nor their caller
origins. Its exclusive cause therefore remains unproven.

A concrete defect in that fixture's producer is reproduced independently: with
clock `874.0000000000003` and the existing 300-second budget, adding the budget
then subtracting the unchanged clock produces `300.0000000000001`. The real
concurrent projectors complete both events and preserve journal invariants, but
the strict existing wait guard rejects both values. A binary-exponent-boundary
fixture also exposes growth with the existing Windows timeout scale of twenty.

Sources reviewed before correction:

- [Python's floating-point documentation](https://docs.python.org/3/tutorial/floatingpoint.html)
  explains representation and rounding errors.
- [Goldberg's floating-point analysis](https://docs.oracle.com/cd/E19957-01/806-3568/ncg_goldberg.html)
  explains rounding across binary exponent boundaries.
- [Amazon's timeout guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
  motivates preserving the caller's time budget; no remote retry defaults are adopted.

The fixture now bounds its computed remaining wait by its unchanged declared
`LONG_TIMEOUT`. It still passes the actual remaining duration, never extends the
deadline, and retains the strict downstream assertion and complete append-once
journal checks. No production code, deadline, scale, or release contract changes.
The guard now prints offending values so another cause will be visible.

The first reproducer raised inside one writer, leaving the other writer unable
to complete under its controlled clock. That own process was stopped, and its
termination is not treated as a successful red regression. The corrected test
observes waits, lets both real writers complete, then asserts the original
contract; unchanged code fails with the exact growing values in 0.89 seconds.

This corrects a reproduced fixture defect. It does not establish that every
Windows latency or wait failure has the same cause.
