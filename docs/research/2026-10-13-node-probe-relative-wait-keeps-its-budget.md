# The Node probe's relative wait keeps its budget

Research and qualification date: 2026-10-13.

CI run 38440080988 failed on Windows Python 3.12 because the actual Node
probe passed `2.0000000000002274` to its process wait. The existing strict
test requires a positive wait no greater than the declared two-second budget.

The production code computed an absolute deadline by adding that budget to a
floating-point monotonic clock, then subtracted the clock to obtain a relative
wait. At a binary exponent boundary, rounding can make the result slightly
larger than the original budget. A deterministic test of the actual
`_ProbeRun._wait_parent` reproduces `2.000000000000001` on unchanged code.

Sources checked before the correction:

- [Python floating-point documentation](https://docs.python.org/3/tutorial/floatingpoint.html)
  explains representation and arithmetic rounding.
- [Goldberg's analysis](https://docs.oracle.com/cd/E19957-01/806-3568/ncg_goldberg.html)
  explains floating-point precision and exponent boundaries.
- [AWS timeout guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
  explains why callers need bounded waits. Its retry defaults are not adopted.

The wait now takes the minimum of the existing declared budget and the
nonnegative remaining duration. The deadline, process-tree ownership, credential
filtering, version checks, cleanup allowance, and strict assertion stay intact.
Tests cover the rounding boundary, a shorter remaining duration, and expiry.

Raising the assertion or adding a comparison tolerance would hide the observed
producer defect. A decimal clock or a new timing framework would add unnecessary
scope. No new numerical limit or architecture contract is introduced.

The earlier projector correction affected a test producer; this defect is in
production. The related process-tree termination wait receives its caller's
absolute cleanup deadline, without this independently declared probe duration,
so applying the Node probe's two-second ceiling there would change its contract.

Live full-health and installed maintenance qualification remain separate from
this regression. This change does not establish a complete point-seven result.
