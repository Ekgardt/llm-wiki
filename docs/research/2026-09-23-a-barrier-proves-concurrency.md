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
