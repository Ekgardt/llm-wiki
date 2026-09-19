# A deadline a test expects to be kept is a hang bound

Date: 2026-09-18
Files: `tests/test_generation_maintenance.py`, `tests/slow_machine.py`

## What was found

`tests/test_generation_maintenance.py::test_cancellation_after_registration_leaves_only_an_unregistered_orphan`
fails on Windows in run 35363057747 of `Ekgardt/llm-wiki`:

```
with pytest.raises(TimeoutError, match="cancel"):
E   AssertionError: Regex pattern did not match.
E     Expected regex: 'cancel'
E     Actual message: 'generation catalog deadline reached'
```

The test wants to prove that a cancellation arriving just after registration leaves an
orphan that is not registered. It arms the cancellation in a wrapper around
`GenerationCatalog._register_validated` and hands the build
`deadline=time.monotonic() + 5`. On the hosted Windows runner the build did not reach the
cancellation inside five seconds, so the deadline fired first — inside
`_acquire_seal_capability`, per the traceback — and the test caught the wrong
`TimeoutError`.

The size of the number is the whole defect, and `tests/slow_machine.py` already measured
the right one — in its own docstring:

> the slowest machine is a hosted Windows runner whose `synchronous=FULL` commits share
> one slow disk with the rest of the matrix: a migration that costs 0.1 s locally
> measured 12.89 s there, and a two-file generation build passed 60 s (runs 34486127382
> and 34491307502, 2026-09-10).

A two-file generation build measured past 60 s on that machine. This test builds a
generation and gives it 5.

Fifteen more deadlines in the same file are the same shape: a real, unfaked clock, a real
generation build or health check, and a literal of 1 or 5 seconds. None of them expects
its deadline to lapse — they assert `status` values, or a different exception
(`RuntimeError, match="injected FTS failure"`). They passed this time by luck.

## Why the existing rule did not catch it

`tests/test_slow_machine.py` walks the test tree for literal hang bounds, but its
`_BOUNDED_CALLS` set is `{"join", "result", "get", "wait", "acquire"}` — a `deadline=`
keyword argument is not one of them. That is deliberate. The `slow_machine` docstring
says so:

> deadline arguments to the code under test are the test's own numbers

The sentence is right about one case and wrong about the other, and it does not
distinguish them. A deadline a test expects to **lapse** — the point of the test is the
refusal — is genuinely the test's own number and must stay a small literal, because the
test pays it on every run. A deadline a test expects to be **kept** is a hang bound
wearing a different keyword: hitting it means something is broken, not slow, and it has
to be sized for the slowest supported machine exactly like a `join`.

## Decision

The sixteen deadlines in `tests/test_generation_maintenance.py` that the tests expect to
be kept take `LONG_TIMEOUT`, which the file already imports and already uses for
`time_budget_seconds`. Raising a generous deadline cannot make a passing test fail, and
none of the sixteen asserts on its lapsing.

One deadline in that file is left alone, for a reason:
`deadline = time.monotonic() + 100.0` at the deadline-expiry test drives a **faked**
`monotonic` (`monkeypatch.setattr(generation_catalog.time, "monotonic", lambda: clock[0])`),
so it is not a wall-clock budget at all, and the test's point is that it expires.

The `slow_machine` docstring is amended to state the distinction rather than exempt every
`deadline=` outright, so the next test to write one knows which kind it is holding.

The guard in `tests/test_slow_machine.py` is **not** extended to `deadline=` keywords.
There are 811 such literals across 39 test files, and no static rule can tell a deadline
meant to lapse from one meant to be kept — that is a property of the assertion beneath
it, not of the call. Mechanically rewriting 811 sites would break every test whose point
is the refusal. The count is recorded here so the owner can decide whether a per-file
sweep is worth doing; it is not something to do blind.

## Sources

- `tests/slow_machine.py`, module docstring, quoted verbatim above: the 12.89 s migration
  and the 60 s two-file generation build, from CI runs 34486127382 and 34491307502.
- `tests/test_slow_machine.py`, `_BOUNDED_CALLS` — the five call names the guard covers.
- The CI evidence: job 105658783888 of run 35363057747, whose traceback shows
  `generation_catalog._check_deadline` raising "generation catalog deadline reached"
  from inside the test's own `register_then_cancel` wrapper — that is, the deadline fired
  during registration, before the cancellation could be seen.
- `grep -c "deadline=time.monotonic() + [0-9]" tests/*.py` → 811 across 39 files,
  measured in this checkout on 2026-09-18.
