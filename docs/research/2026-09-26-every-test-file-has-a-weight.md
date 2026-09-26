# Every test file has a weight

Date: 2026-09-26. Audit 2026-09-26, finding C-13 (541 of 668 test files had no
shard weight; stale scheduler constants and CI comments).

## What was wrong

`tests/shard_plan.py` packs test files onto the four CI shards by their weight in
`tests/shard_weights.json`; a file without a weight cost a flat 5 s. Measured on
2026-09-26 from the JUnit artifacts of CI run 36220987257 (commit 0098c495): the
suite had 705 files, 578 of them unweighted; per file the median was 0.33 s and the
mean 11.2 s, and unweighted files ran up to 102 s. The twenty Windows shards of that
run took between 809 s and 1996 s — the flat default had stopped balancing them.

The Windows task script also wrote its hour limits three times (two
`-ExecutionTimeLimit` literals and a `LimitHours` spec table), and comments carried
figures that drift: "about 3.2 h and 4.9 h" for the passes (this machine now
computes 3.27 h and 5.16 h), "shards that normally take 22-26 minutes".

## Decision

- `python -m tests.shard_plan --refresh <artifacts>` rewrites the table from JUnit
  reports: each file's weight is the slowest job that ran it; files that no longer
  exist leave the table. A module skipped whole at collection (empty `classname`)
  counts too. The table now weighs all 713 files; the plan puts 1997 s of
  slowest-job time on each shard.
- `python -m tests.shard_plan --weigh <files>` measures new test files here.
- A file still unweighted costs the table's mean, not 5 s.
- Guard: `tests/test_every_test_file_has_a_weight.py` fails when a test file has
  no weight or a weight names a deleted file, and names the command to run.
- `install-scheduled-tasks.ps1` has one `$LimitHours` table used for registration
  and for the spec check; a guard fails on any hour literal next to `-Hours` or
  `LimitHours =`, and the table must equal `install_control.SCHEDULER_LIMIT_HOURS`.
  Comments name the functions that compute the bounds instead of figures.

## Source

Wikipedia, Longest-processing-time-first scheduling,
https://en.wikipedia.org/wiki/Longest-processing-time-first_scheduling, fetched
2026-09-26: "Order the jobs by descending order of their processing-time, such that
the job with the longest processing time is first." / "Schedule each job in this
sequence into a machine in which the current load (= total processing-time of
scheduled jobs) is smallest." For identical machines LPT is within
"(4m - 1)/(3m) = 4/3 - 1/(3m)" of the optimum — a guarantee that holds only for the
processing times it is given, which is why the weights must be measured, not
defaulted.

## Files

- `tests/shard_plan.py`, `tests/shard_weights.json`
- `tests/test_every_test_file_has_a_weight.py`
- `scripts/install-scheduled-tasks.ps1`, `scripts/install_control.py`
- `tests/test_the_scheduler_outlasts_the_pass.py`,
  `tests/test_the_weekly_task_outlasts_its_pass.py`,
  `tests/test_a_changed_task_setting_reaches_an_installed_machine.py`
- `.github/workflows/tests.yml`
