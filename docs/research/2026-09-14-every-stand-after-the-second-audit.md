# Every stand after the second audit

Dated 2026-09-14. The owner merged PR 35 (`5940eb9`) and asked for a full run of
every measurement. This note settles how it is taken, before it is taken.

## The facts

- `benchmark/` holds thirteen `run_*.py` stands plus `longmemeval_judge.py` and
  `aggregate_code_parity.py`. The 2026-09-13 pass
  (`docs/REPORT-2026-09-13-stands.md`) ran eleven of them and left out
  `run_longmemeval.py` and `run_consolidation.py`, the two that spend model tokens.
- The 500-question hybrid run of 2026-09-13/14 stopped at 286 questions: two
  workers were taken down for memory. Measured then: the reranker holds 2.6 GB, the
  encoder 1.2 GB, a worker's peak with the vector build is about 5 GB, on a 16 GB
  machine where the editor and the agent service hold about 4 GB. The job that took
  them down was the agent's own background task, not the kernel.
- The nightly timer fires at 03:00 and its pass loads the same models; a stand and
  the pass sharing four cores is what failed two process-and-deadline tests on
  2026-09-13 (the same report, "Evidence").
- A LongMemEval run imports `scripts/` from the checkout for every question
  (`runs-import-the-checkout`), so the checkout is not edited while it runs.

## Practice on this date

- One process at a time, nothing else loaded, cold and warm reported apart
  (`docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`
  and its sources).
- Keep a benchmark's host quiet: scheduled jobs are paused for the measurement
  window and restored after it, including on failure.

## The decision for this run

- All stands run sequentially in one `systemd-run --user` unit, outside the agent's
  task limits, from the checkout at `5940eb9`. The nightly and weekly timers are
  stopped for the unit's life and started again by its `ExecStopPost`, which runs
  whether the unit ends cleanly or not.
- Offline stands use `MEMORY_LLM_PROVIDER=fake` at their documented defaults. The
  parity stand runs three times and is aggregated.
- LongMemEval: first `--full --retrieval-only` (evidence coverage, no model calls),
  then `--full --seed 101 --concurrency 1 --provider-timeout 600` with the reader the
  owner configured on this machine (`MEMORY_CLAUDE_MODEL=claude-sonnet-5`), then the
  judge. One worker, because two do not fit in memory.
- `run_consolidation.py` at its defaults with one worker, and the judge over both of
  its arms.
- A provider probe runs first; if the reader cannot answer, the model stands are
  skipped rather than filled with error rows.
- Every artifact goes to `cache/benchmarks/full-2026-09-14/`, which is ignored. Only
  counts and rates reach the report; nothing derived from the private vault is
  committed.

Files: `docs/research/2026-09-14-every-stand-after-the-second-audit.md`.
