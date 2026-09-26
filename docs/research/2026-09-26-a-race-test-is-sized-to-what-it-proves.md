# A race test is sized to what it proves

Date: 2026-09-26. Audit 2026-09-26, finding B-27 (unstable tests paint main red), the
two parts left open.

## Facts

- `test_multiprocess_status_reads_remain_coherent_during_claim_and_complete` timed out
  on a Windows runner after 337 s (CI run 36170944200, job 108190034067:
  `future.result(timeout=LONG_TIMEOUT)` raised `TimeoutError`). Its call time on the
  Windows jobs of the last six `work` runs: 82-229 s, against the 300 s hang bound. It
  is not a hang; it is a workload near its bound.
- Measured here 2026-09-26 on Linux: the old workload (four writers of six tasks, two
  readers of 80 reads) took 20 s idle and 147 s while another suite ran; one claim took
  0.3 s idle and 2.3-3.5 s loaded. A profile of three claim/complete/status rounds under
  load: 138 SQLite commits took 8.5 s of 13 s. Each blackboard write is a fenced Markdown
  transaction with about fifteen commits, each synced to disk.
- The parallel telemetry writers' failure (CI run 35941975284, `database is locked` in
  `_ensure_schema`) was already fixed on this branch by 425a8bd5 (schema under its own
  write lock); nothing is left of it.

## Source

SQLite, PRAGMA documentation, fetched 2026-09-26 from https://www.sqlite.org/pragma.html:
"When synchronous is FULL (2), the SQLite database engine will use the xSync method of
the VFS to ensure that all content is safely written to the disk surface prior to
continuing." The product's databases use FULL by contract (CLAUDE.md, Stage 2), so a
write's time is the disk's sync time, which is what varies across runners and load.

## Decision

- The test proves coherence of status reads while claims and completions land. It keeps
  two concurrent writers and two concurrent readers, and every interleaving that
  assertion needs; it no longer asks for a throughput: two writers of three tasks, 24
  reads. 3.4 s idle here, a sixth of the old load.
- Not done: making blackboard writes cheaper (fewer commits per append). That is a change
  to the reliability core's write path and belongs in its own decision, with its own
  measurement on the live vault.
- No recurrence guard by stopwatch: a hang bound is not a time budget
  (`docs/research/2026-09-10-a-timeout-is-a-hang-bound-not-a-stopwatch.md`).

## Files

- `tests/test_blackboard.py`
