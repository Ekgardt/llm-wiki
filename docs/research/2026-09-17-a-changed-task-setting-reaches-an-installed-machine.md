# A changed task setting reaches a machine that is already installed

Dated 2026-09-17. Finding I-A4 of the third audit (medium, confirmed by reading and by a
stand-in run). The research before the fix.

## What was found

- On 2026-09-14 the Windows nightly and weekly tasks got time limits of 3 and 5 hours,
  because a one-hour limit killed the pass inside its own bounds.
- The fix lives only in the registration call of `scripts/install-scheduled-tasks.ps1`.
  Whether registered tasks are "equivalent" is decided by `Test-LLMWikiScheduledTasks`
  from the kind, the three paths, the trigger and the principal. The time limit is not
  looked at.
- The Python side describes the tasks by a specification
  (`install_control.render_windows_task_spec`) that does not contain the limit either. An
  update compares the specification it wants with the one it finds; they are equal, so
  `_v2_apply_resource` marks the resource verified and never calls the script.
- So a machine installed before 2026-09-14 keeps its one-hour tasks for ever, with a green
  installer. systemd does not have this problem: `TimeoutStartSec` is part of the rendered
  unit, so the unit text changes and the update rewrites it.

## Practice on this date

- The stored form of the limit: "The format for this string is PnYnMnDTnHnMnS … (for
  example, PT5M specifies 5 minutes …). A value of PT0S will enable the task to run
  indefinitely." (Microsoft Learn, `TaskSettings.ExecutionTimeLimit`,
  <https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit>,
  fetched today.) .NET reads that form with `System.Xml.XmlConvert.ToTimeSpan`, so the
  comparison does not depend on whether the scheduler writes `PT3H` or `PT180M`.
- The cmdlet side: "Specifies the amount of time that Task Scheduler is allowed to complete
  the task." (`New-ScheduledTaskSettingsSet -ExecutionTimeLimit`,
  <https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset>,
  fetched today.)
- The control plane's own rule for every scheduler: what is installed is one of the
  candidate specifications, and exactly one (`_installed_projection` refuses two). A new
  specification therefore has to be told apart from the old one on the machine itself, in
  both directions — otherwise tasks registered between 2026-09-14 and today (three hours,
  old specification) would answer to both and every update would stop as ambiguous.

## The decision

- The specification carries what the contract promises: `"spec": 2` and `limit_hours` for
  each task. The old form (no `spec`, no limits) stays readable, because installed
  manifests record it.
- The script learns `-SpecVersion`. Python passes 1 for a recorded old specification and 2
  for the current one; by hand the default is 2.
  - Version 2 registers the tasks with a marker at the end of the description,
    `[llm-wiki-task-spec:2]`, and calls tasks equivalent only when the marker is there and
    the limit equals the contract's.
  - Version 1 calls tasks equivalent only when the marker is absent. That keeps uninstall
    and rollback of an old record working, and makes the two specifications exclusive.
- An update of an old machine then finds "old specification installed, new one wanted",
  removes the old tasks and registers the new ones through the existing projection writer.
  Nothing else in the transaction changes.
- The test runs the real script function under PowerShell with stand-ins for the
  Task Scheduler cmdlets, and the Python path with a stand-in runner that tells the two
  versions apart. No Windows machine was available; the stored form of the limit is taken
  from the Microsoft page above, and the parse is format-tolerant for that reason.

Files: `scripts/install-scheduled-tasks.ps1`, `scripts/install_control.py`,
`tests/test_a_changed_task_setting_reaches_an_installed_machine.py`,
`tests/test_install_control.py`,
`docs/research/2026-09-17-a-changed-task-setting-reaches-an-installed-machine.md`.
