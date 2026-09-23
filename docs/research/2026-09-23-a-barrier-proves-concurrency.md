# A barrier proves concurrency; a stopwatch measures the machine

Dated 2026-09-23. Files: `tests/test_code_graph.py`,
`docs/research/2026-09-23-a-barrier-proves-concurrency.md`.

## What was found

- `test_external_version_probes_run_concurrently_with_short_timeouts` asserted
  `elapsed < 0.35` for three probes that each sleep 0.15 s: concurrent is ~0.15 s,
  sequential is ~0.45 s. On CI run 35857662331 (Windows, py3.13 shard 4, commit
  961c78b9) the same three probes took 0.60 s under four parallel shards, and the test
  failed with nothing wrong in the code. The threshold measured the runner.
- The same form stands in seven other tests (`test_install_pyright.py` 608 and 2406,
  `test_workspace_revision.py` 1342, 2385 and 2424, `test_context_noise.py` 945,
  `test_doctor.py` 1928): sub-second wall-clock ceilings. Each states a different
  property (a deadline honoured, a cache hit, a bounded scan); none has failed on CI in
  this branch's runs that I have seen, so each is left to its own evidence.

## Practice on this date

- Concurrency is proved by a rendezvous, not by a clock: `threading.Barrier(n)` is
  released only when all `n` parties are waiting at once, and a party that waits alone
  past the timeout breaks the barrier for everyone
  ([Python docs, `threading.Barrier`](https://docs.python.org/3/library/threading.html#barrier-objects)).

## The decision

The three probes meet at one `Barrier(3, timeout=5)`; the test asserts the barrier is
not broken and that each probe was given the 2-second timeout. No clock is read.

## Sources

- [threading — Barrier objects](https://docs.python.org/3/library/threading.html#barrier-objects) — fetched 2026-09-23.
- CI run 35857662331, job 107170071717, 2026-09-23 12:18 UTC.

## Addendum, later on 2026-09-23: a ceiling names the alternative

Files: `tests/test_lsp_process.py`.

CI run 35860367016 (Windows, py3.10 shard 2, commit 3352215c) failed
`test_four_delayed_owner_acl_starts_share_deadline_and_leave_no_leaks` at 10.906 s
against a ceiling of the 10 s startup wait plus 0.75 s. Five tests in that file bound
a start that gives up at the startup wait by the same 0.75 s margin. The outcomes the
bound must exclude are the child's 30 s sleep and four serialised waits (40 s), so the
ceiling is now twice the startup wait, one constant, `_STARTUP_CEILING_SECONDS`: a
retry that should not have happened (5 s more) and a start that waited on the child
are still caught, and a slow runner is not.
