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

## Follow-up, 2026-10-01: measured drift and a quadratic security scan

PR 52 head `f384b51d`, CI run `36873543509`, retained all 36 JUnit/progress
artifacts. Windows Python 3.10/3.12 shard 3 reached the existing 60-minute job
deadline. The other Windows shards completed. Full file coverage alone did not
keep the old costs current: operational-journal migration was weighted 18.77 s
but measured 513.7 s; breadcrumb delivery was weighted 11.66 s but measured
367.2 s. Replaying the old partition with the new per-file maxima puts 4649.3 s
on shard 3, against 2368.5–2915.4 s on the others.

That imbalance was not the whole cause. Python 3.10's progress file ends at
`test_only_daily_archiver_has_directory_publication_exception`, absent from its
interrupted JUnit; the same test took 299.675 s on Python 3.11. Each assignment
called `ast.get_source_segment` on the whole module again. The security scanner
now reuses `code_extractor._line_offsets`: encode once, locate Python newline
boundaries once, slice by AST UTF-8 byte offsets. The original invariant and its
assertion are unchanged. Replacing it with a regex, dropping files, unparsing
the AST, or raising the timeout would change its meaning or hide its cost.

The existing refresh command consumed all 36 reports, including other operating
systems so Windows skips do not erase their costs. It retains every one of 811
test files and gives four predicted totals of 3197.0 s. Interrupted reports are
partial observations, not complete runs; maxima also include complete runs of
the same files. The security file's old measured cost is conservative until its
new native measurements arrive. No timeout, shard count, or security requirement
changed, and these predictions are not a Windows pass.

Validation: the real repository scan took 52.09 s before and 4.31 s after on the
same local interpreter. All 62 security tests pass. A regression counts repeated
whole-source work rather than imposing a machine-dependent timing threshold:
the old helper returns the correct text but scans 153 characters for a 51-character
module, failing the one-scan bound; the new helper passes. Text extraction agrees
with the standard library for Unicode byte columns, multiline assignments/calls,
tabs/form feeds, and LF/CRLF/CR. Positive and negative publication cases remain
checked. Final full native CI, especially Windows, is still required.

Primary sources checked on 2026-10-01:

- [Python AST locations and source segments](https://docs.python.org/3/library/ast.html#ast.get_source_segment):
  AST columns are UTF-8 byte offsets, not character indices; location semantics
  constrain the equivalent extractor. The local standard-library implementation
  also confirms repeated line splitting.
- [pytest duration reporting](https://docs.pytest.org/en/stable/how-to/usage.html#profiling-test-execution-duration):
  use retained per-test measurements and JUnit data instead of inferring a hang
  from a job's overall elapsed time.
- [GitHub job timeout contract](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idtimeout-minutes):
  the job deadline cancels unfinished work. Preserve the existing bound and fix
  the measured work and partition rather than declaring cancellation a test pass.
