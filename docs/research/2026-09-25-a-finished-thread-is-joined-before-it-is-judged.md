# A finished thread is joined before it is judged

Date: 2026-09-25. Found by the clean full run of 2026-09-25 (9252 passed, 2
failed); the test passed alone three times out of three.

## Facts

- `tests/test_lsp_process.py::test_caller_restart_failure_keeps_deadline_and_retains_cleanup_owner`
  waits for `caller_finished`, which the thread sets in its own `finally`, and
  then asserts `not caller.is_alive()`. Between setting the event and leaving
  `run()` the thread is still alive, so under load the assertion can meet it in
  that gap: `assert not True` in the full run.

## Source

- Python `threading`, https://docs.python.org/3/library/threading.html#threading.Thread.is_alive
  (fetched 2026-09-25): `is_alive()` is true "until just after the `run()`
  method terminates"; after `join(timeout)`, "you must call `is_alive()` ... to
  decide whether a timeout happened".

## Decision

- The test joins the thread (bounded by the same short timeout) before it asks
  whether the thread is alive. The event still proves the caller returned in
  time; the join proves the thread is gone.

## Files

- `tests/test_lsp_process.py`
