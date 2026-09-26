# No test gives real work a literal deadline

Date: 2026-09-26.

## What was wrong

A clean full run on the merged branch failed one test:
`test_code_navigation.py::test_result_construction_crossing_deadline_never_publishes_facts`
raised `workspace revision deadline reached` inside its setup helper `_revision`,
which handed a directory walk `deadline=time.monotonic() + 5`. Alone it passed three
times out of three in 0.3 s. Under the full suite's load the walk did not finish in
5 s. The test was not about that deadline at all.

Measured: 770 keyword deadlines of the form `time.monotonic() + N` with N ≥ 1 in
`tests/`; 572 of them in functions that are not about time running out. Each is a
hang bound sized for one machine — the class `test_slow_machine.py` already forbids
for `join`/`wait`/`result`/`get`/`acquire`, but not for `deadline=`.

## Decision

- Every such deadline in a function that does not speak of time now names
  `SHORT_TIMEOUT` (30 s, scaled by `LLM_WIKI_TEST_TIMEOUT_SCALE`). A passing test
  is exactly as fast; only a stalled one waits longer before it fails.
- A literal deadline stays where the enclosing function's name says the test is
  about time (deadline, timeout, timed, expire, late, elapse, budget, hang, slow,
  stall, stuck, overrun, cancel, interrupt, in_time): there the number is the point.
  A first pass without `timed` and `interrupt` turned two benchmark tests that wait
  their deadline out from 2 s into 30 s; they keep their literal.
- Guard: `test_slow_machine.py::test_no_test_gives_real_work_a_literal_deadline`
  finds every literal `deadline=time.monotonic() + N` (N ≥ 1) outside such a
  function. On the old `test_code_navigation.py` alone it finds 129.

## Source

pytest documentation, "Flaky tests", fetched 2026-09-26 from
https://docs.pytest.org/en/stable/explanation/flaky.html:
"Overly strict assertions can cause problems with floating point comparison as well
as timing issues."

Conclusion (mine): a fixed few-second budget on real I/O is an overly strict timing
assumption; the suite already scales its waits for a loaded machine, and the
deadlines now use the same scaled names.

## Files

- `tests/test_slow_machine.py` (guard)
- 37 test files under `tests/` (literal deadlines renamed; each checked to be the
  same syntax tree as before apart from those deadlines)
