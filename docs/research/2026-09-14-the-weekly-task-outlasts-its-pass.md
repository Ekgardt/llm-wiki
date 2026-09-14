# The weekly task outlasts its pass

Dated 2026-09-14. Item 0.4 of `docs/AUDIT-2026-09-14-2.md`: this morning's fix
(`docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md`) raised the nightly
task's limit and left the weekly one where it was. The research before the fix.

## What was found

- `scripts/install-scheduled-tasks.ps1` registers `LLMWiki-Weekly` with
  `-ExecutionTimeLimit (New-TimeSpan -Hours 2)` — 7 200 s.
- `scheduled_weekly._run_weekly_body` runs, in order: the compile idle wait (30 s),
  the whole nightly pass (`scheduled_nightly.worst_case_seconds()` = 8 670 s on this
  date), five subprocess steps from `_script_steps` (120 + 60 + 120 + 300 + 1 200 =
  1 800 s), the opt-in contradiction check (1 800 s), then two in-process steps.
  The nightly pass alone is above the limit.
- Of the in-process steps, `_build_tiers` is deterministic (`use_llm=False`) and took
  under a second in every weekly log on disk (2026-08-30, 09-06, 09-13).
  `_reflect_candidates` calls the model once per candidate page, each call bounded by
  `llm_client._timeout_s()` (90 s by default), with **no bound on the number of
  pages**: the weekly pass has no worst case at all. On the live vault today
  `find_reflection_candidates()` returns 0 (run read-only), so it has not bitten.
- The code graph: `_reflect_candidates` ← `_reflect` ← `_run_weekly_body` ←
  `run_weekly`; nothing else calls it. Tests reach `_script_steps` and `run_weekly`.
- A killed reflection is safe to cut between pages: each page is written through
  `mutate_knowledge` with a hash precondition.

## Practice on this date

- Microsoft: a task is stopped when its `ExecutionTimeLimit` passes
  ([TaskSettings.ExecutionTimeLimit](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit)).
- The rule this codebase applies to its own steps: a child's budget ends before its
  parent's kill (`scheduled_nightly.py`, audit OPS-10; `prune_generations`
  `--budget-seconds`). An unbounded loop of model calls inside a bounded task breaks
  that rule by construction.

## The decision

- Reflection gets a budget, `REFLECTION_BUDGET_SECONDS = 1800`, checked before each
  page's model call; pages left over wait for next week and are named in the log.
- `scheduled_weekly.worst_case_seconds()` sums the pass from the constants it runs
  with: the idle wait, the nightly bound, the step timeouts, the contradiction check,
  the reflection budget plus one call's default timeout. On this date that is
  14 190 s, 3.9 hours.
- The weekly task's limit becomes **5 hours**, with the same kind of margin the
  nightly got; a test reads it from the installer and fails if the bound outgrows it.
- An operator who widens `MEMORY_LLM_TIMEOUT_S` widens one call past the default; the
  bound uses the default, as the nightly bound does for its model steps' timeouts.

Not changed here: at 04:00 on Sunday a nightly pass can still be running, and the
weekly then skips (the shared fence). That is an existing scheduling choice, recorded
for a later item, not part of this fix.

Why not the alternatives:

- **`PT0S`, no limit.** A hung pass would hold its lease forever on Windows.
- **Raise the limit only.** The reflection loop would still have no bound to compare
  the limit against.

Files: `scripts/install-scheduled-tasks.ps1`, `scripts/scheduled_weekly.py`,
`tests/test_the_weekly_task_outlasts_its_pass.py`,
`docs/research/2026-09-14-the-weekly-task-outlasts-its-pass.md`.
