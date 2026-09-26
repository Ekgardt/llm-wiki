# An ownership probe says why it measured nothing

Date: 2026-09-26. `main` CI run 36252530355 (the merge of PR 43) failed one job,
`timing::focused::pyright-windows`, at the step "Correctness benchmark gate".

## Facts

- The correctness figures were all perfect (definitions 200/200, references and
  calls F1 1.0, citations 800/800, crash recoveries 20/20). The gate failed because
  one of four process-ownership scenarios, `timeout`, reported
  `{"available": false, "orphan_count": null}`; one unmeasured scenario marks the
  whole report `evidence_complete: false`, and then every gate reads "not measured".
- The report said nothing about why. `_attempt_ownership_scenario` caught the
  failure and dropped it.
- The same tree passed this job on the PR head (b6247a61). Over the last 40 runs
  of the workflow this job failed twice; the other failure (2026-09-25) was a
  different test. So this is intermittent, not a regression of the merge.
- Reproduced locally with the real Pyright 1.1.411 on Linux, idle and with 8 busy
  processes on 4 cores, instrumenting every attempt: three runs, and every
  `timeout` attempt was dispatched and ended in `TimeoutError` — measured.
- In the CI run the `cancellation` scenario, which runs after `timeout`, was
  measured. A probe that was never sent waits for send evidence until the outer
  deadline, which would have left `cancellation` no time; so on CI the cause was
  not an unsent request. That leaves: the server answered first five times
  (`raced`), the probe ended in the wrong terminal, or preparing or resetting the
  process raised.

## Not done, and why

I first made an unsent probe retryable and ended the send-wait at completion. The
existing test `test_ownership_waits_for_post_write_evidence_after_fast_completion`
proves the send evidence can arrive after the request completes, so that early exit
was wrong; both changes were withdrawn.

## Decision

The root cause is not established. What is certain: the report must say it. Each
attempt that measured nothing now records its reason — `raced`, or the exception
class with its cause (`RuntimeError:ValueError`) — and every unavailable scenario
adds `{"phase": "ownership:<scenario>", "code": <reason>}` to the report's `errors`
(the schema already allows any phase and code). The next failure names its cause.

## Sources

- PEP 3134, fetched 2026-09-26, https://peps.python.org/pep-3134/ — "The
  `__cause__` attribute on exception objects is always initialized to `None`. It is
  set by a new form of the `raise` statement: raise EXCEPTION from CAUSE". The
  wrong-terminal path raises `from` the probe's error, so the cause is recoverable.
- Martin Fowler, "Eradicating Non-Determinism in Tests", fetched 2026-09-26,
  https://martinfowler.com/articles/nonDeterminism.html — "Place any
  non-deterministic test in a quarantined area. (But fix quarantined tests
  quickly.)" Conclusion (mine): quarantining this gate would hide the ownership
  evidence it exists to require; recording the cause is what makes a quick fix
  possible.

## Files

- `benchmark/run_code_navigation.py`
- `tests/test_an_ownership_probe_says_why_it_measured_nothing.py`
