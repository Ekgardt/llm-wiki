# The scheduler outlasts the pass

Dated 2026-09-14. Item 3.7 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

- `scripts/install-scheduled-tasks.ps1` registers `LLMWiki-Nightly` with
  `-ExecutionTimeLimit (New-TimeSpan -Hours 1)`.
- The nightly pass's own bounds add up to far more. Summed from
  `scheduled_nightly.py` on 2026-09-14: pre-compile steps 1 200 s (capture adoption
  120, reclaim 180, queue 600, episodes 300), the idle wait for a running compile
  30 s, compile and fact keys 720 s, the compile-finished wait
  `COMPILE_WAIT_SECONDS` 1 800 s, the post-compile steps 3 960 s, the generation
  refresh `NIGHTLY_GENERATION_BUDGET_SECONDS` 900 s and the health report 60 s —
  about **8 670 s, 2.4 hours**.
- When the limit is reached Task Scheduler stops the task: the pass is killed without
  releasing its scheduled-owner lease or recording its result, and the next night
  finds a lease to wait out. The weekly task's 2-hour limit is above the weekly's
  steps (1 800 s, plus 1 800 s when contradictions are enabled).
- Linux (systemd `oneshot`, no timeout) and macOS are not affected.

## Practice on this date

- Microsoft: a task "will be stopped" when its execution time limit passes; the
  default is 72 hours, `PT0S` removes the limit, and the setting is bypassed for a
  task started on demand
  ([TaskSettings.ExecutionTimeLimit](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit),
  [New-ScheduledTaskSettingsSet](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset?view=windowsserver2025-ps)).
- The rule this codebase states for its own steps: a child's budget ends before the
  parent's kill (`scheduled_nightly.py`, audit OPS-10). The scheduler is the outermost
  parent.

## The decision

- `scheduled_nightly.worst_case_seconds()` states the pass's bound as the sum of its
  parts, from the same constants the pass runs with.
- The nightly task's limit becomes **3 hours** — above 2.4 h with room for interpreter
  start-up and logging, and still a bound, unlike `PT0S`.
- A test reads the limit from the installer and fails if the pass's bound ever exceeds
  it, so a new step that lengthens the night cannot silently outgrow the scheduler.

Why not the alternatives:

- **`PT0S`, no limit.** A hung pass would then hold its lease forever on Windows.
- **Shorten the night.** Each step's bound was measured and justified on its own; the
  scheduler's number was the one never derived.

Files: `scripts/install-scheduled-tasks.ps1`, `scripts/scheduled_nightly.py`,
`tests/test_the_scheduler_outlasts_the_pass.py`,
`docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md`.
