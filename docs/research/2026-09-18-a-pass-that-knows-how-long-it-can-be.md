# A pass that knows how long it can be

Date: 2026-09-18. Trigger: the third audit's M-B2, M-B3, M-B4, I-A14 and I-A15 —
every one of them about the scheduled passes and the bounds they claim. The owner
delegated the decisions; this note records them.

## What was found (by reading and by computing, in a temp root)

- **M-B2.** The steps whose child stops at a budget leave a margin of
  `STEP_START_MARGIN_SECONDS = 120` for the last model call. One call is not one
  provider: in auto mode `llm_client._candidate_order("")` is five providers and
  a call walks them in turn, each up to `_timeout_s()` (90 s by default), so one
  `call_llm` can take 450 s. The margin is then a fiction, and the step is killed
  mid-call. `MEMORY_LLM_TIMEOUT_S` widens every one of those timeouts and nothing
  in the sum sees it.
- **M-B3 / I-A14.** `scheduled_nightly.worst_case_seconds()` says "every step's
  timeout and every wait". Computed in a temp root it returns 8670 s. It omits
  `_update_code` (fetch 120 s + `uv sync` 600 s + several 60 s git calls,
  `self_update`), the telemetry compaction, the report pruning, and it uses the
  `COMPILE_WAIT_SECONDS` constant while the pass itself honours
  `MEMORY_COMPILE_WAIT_SECONDS`. Windows is the only scheduler with a limit at
  all (3 h nightly, 5 h weekly); launchd and cron have none, so on macOS and on a
  cron fallback nothing bounds a pass except the pass itself.
- **M-B4.** The unit is `Type=oneshot` with no `KillMode=`. When the nightly
  defers a compile that is still running past the wait bound, `scheduled_nightly`
  exits and systemd kills what is left in the unit's cgroup — the compile, which
  `spawn_detached` left in that cgroup (a new session is not a new cgroup). The
  next pass does not count the loss: `_compile_died_this_pass` compares against a
  start stamp from *this* pass, and the deferred compile started in the previous
  one.
- **I-A15.** `logs/scheduled-{nightly,weekly}.log` (launchd) and
  `logs/cron-{nightly,weekly}.log` are append-only and named by nothing:
  `MAINTENANCE_REPORT_PATTERNS` does not match them, so the nightly retention
  pass never touches them. They grow for the life of the install.

## Sources

1. systemd, `systemd.kill(5)` on `KillMode=`: `control-group` — "all remaining
   processes in the control group of this unit will be killed on unit stop";
   `process` — "only the main process itself is killed (not recommended!)";
   `none` — "no process is killed (strongly recommended against!)"; "Defaults to
   control-group"; and the warning: "Note that it is not recommended to set
   KillMode= to process or even none, as this allows processes to escape the
   service manager's lifecycle and resource management, and to remain running
   even while their service is considered stopped and is assumed to not consume
   any resources."
   https://github.com/systemd/systemd/blob/main/man/systemd.kill.xml (fetched
   2026-09-18)
2. This repository: `docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md`
   (the rule that the scheduler's limit sits above the pass's own bounds),
   `docs/research/2026-09-14-every-budget-inside-its-step.md` and
   `2026-09-17-a-step-no-provider-answered-is-not-green.md` (the margin rule this
   note corrects), `knowledge/notes/automatic-code-update-decision.md` (the
   nightly update).
3. Measured here, in a temp root: nightly `worst_case_seconds()` 8670 s, weekly
   14190 s, against the 10800 s and 18000 s Windows limits.

## Decisions

1. **A margin covers a whole call, not one provider.** `llm_client` gains
   `worst_case_call_seconds()`: the timeout in force times the number of
   candidates in force — one when the operator forces a provider (which is what
   an installed scheduler has), five in auto mode. The two steps whose child
   stops at a budget (`episodes`, `fact_keys`) take that as their margin instead
   of the flat 120 s, so a call that runs to its own limit finishes inside the
   step rather than being killed. An operator's `MEMORY_LLM_TIMEOUT_S` now widens
   the margins and the sum with it, instead of silently breaking them.
2. **The sum counts everything the pass can do.** `worst_case_seconds()` adds
   `self_update.WORST_CASE_SECONDS` (its own subprocess timeouts, named one by
   one), `MAINTENANCE_TAIL_BUDGET_SECONDS` for the two tail tasks that are
   bounded by work rather than by time (a telemetry compaction of at most
   `DEFAULT_MAX_DELETE = 1000` rows under a 5 s busy timeout, and a retention
   pass over at most `REPORT_RETENTION_FILES = 60` reports), and it reads the
   compile wait as configured. The docstring says exactly that.
3. **The pass bounds itself.** Between steps the pass checks that it has not
   outlived `worst_case_seconds()`; if it has, it stops there, records the reason
   and releases its fence. This is the bound launchd and cron do not give it, and
   it is the same on every platform. The Windows limits rise to 4 h and 6 h so
   that the scheduler still outlasts the honest sum (the guard tests keep that
   relation).
4. **A deferred compile is a loss, and it is counted.** `KillMode=process` is
   what systemd itself says not to do, so the unit is left alone. Instead the
   pass records the deferred compile's start stamp; the next pass sees that it
   never finished and reports it as a failed compile, once, by name. The nightly
   log line says plainly that a service manager may stop the compile with the
   unit.
5. **The scheduler logs are trimmed, not deleted.** They are the file the
   scheduler redirects into, and this pass is the process writing them, so
   nothing may rename or unlink them: the nightly keeps the last
   `SCHEDULER_LOG_KEEP_BYTES` of each and truncates in place, and it says how many
   bytes it dropped. A file it cannot open for writing (a Windows handle held with
   no sharing) is left alone.

Files: `scripts/llm_client.py`, `scripts/self_update.py`,
`scripts/scheduled_nightly.py`, `scripts/scheduled_weekly.py`,
`scripts/maintenance_helpers.py`, `scripts/install-scheduled-tasks.ps1`,
`tests/test_a_pass_that_knows_how_long_it_can_be.py`,
`tests/test_the_scheduler_outlasts_the_pass.py`,
`tests/test_the_weekly_task_outlasts_its_pass.py`.
