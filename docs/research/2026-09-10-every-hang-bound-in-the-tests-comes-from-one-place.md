# Every hang bound in the tests comes from one place

Date: 2026-09-10. Trigger: audit finding OPS-04. `tests/slow_machine.py`
said "every wait comes from here; no test carries a literal", and an AST
pass over `tests/*.py` found 390 literal bounds on `join`, `result`, `get`
and `wait` calls — 354 of them positive (the test expects the thing to
happen and the number is only a hang bound), 36 negative (the test expects
the bound to elapse: `assert not event.wait(1)`, `pytest.raises(TimeoutError)`).
`tests/test_lsp_process.py` alone carried 127 positive bounds, most of them
`event.wait(1)` and `thread.join(5)` — the exact shape that turned the
Windows runner red on 2026-09-09 (the earlier note,
`docs/research/2026-09-10-a-timeout-is-a-hang-bound-not-a-stopwatch.md`).

## Sources

1. CPython `Lib/test/support/__init__.py`: `SHORT_TIMEOUT` is "the timeout
   for tests that should complete quickly", `LONG_TIMEOUT` for tests that
   "may take a long time"; both are for "waiting for something that should
   happen", and a test never asserts on their elapsing. The earlier note
   cites the text. https://github.com/python/cpython/blob/main/Lib/test/support/__init__.py
2. The same note's runner evidence (actions/runner-images issues 12647,
   8755, 3577): a hosted Windows runner records a 0.1 s operation at 12.9 s.
3. The classifier written for this note (`tests/test_slow_machine.py`,
   `_literal_hang_bounds`): a literal on `join`, `result`, `get` or `wait`
   is a hang bound unless the call sits under `not`, a comparison with
   `False`/`None`, or `pytest.raises` — then it is an expectation that the
   bound elapses, and it stays a small literal by design, because that test
   pays the bound on every run.

## Decision

1. Positive hang bounds take `SHORT_TIMEOUT` when the literal was at most
   30 s and `LONG_TIMEOUT` above that. A bound only matters when the thing
   never happens, so a larger bound changes no passing test.
2. A `wait` is a hang bound only when the test asserts the event arrives
   (`assert e.wait(n)`). A `wait` whose result is discarded, branched on, or
   assigned and later compared with `False` is the test's own pause — the
   first rewrite treated those as hang bounds and six tests failed, because
   the pause itself was the point (`archive_done.wait(0.25)` then
   `assert … is False`; `second_construction.wait(0.5)` as a settling pause).
   Those 21 sites keep their literal. `join`, `result` and `get` bounds are
   hang bounds unless the test expects them to elapse.
3. Deadline arguments to the code under test (`search(..., timeout=5)`,
   `deadline=`) are the test's own numbers, not waits; out of scope.
4. `tests/test_slow_machine.py::test_no_test_carries_a_literal_hang_bound`
   keeps the count of positive literals at zero, so the class cannot grow
   back one test at a time. Result: 330 sites in 26 files migrated.
5. `tests/slow_machine.py`'s docstring states the rule as it is.

Files: `tests/slow_machine.py`, `tests/test_slow_machine.py`, and every
`tests/test_*.py` the rewrite touches (listed by the commit), `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
