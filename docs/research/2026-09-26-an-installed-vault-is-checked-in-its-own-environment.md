# An installed vault is checked in its own environment

Date: 2026-09-26. CI run 36246652666 on PR 43 (head 3faea074): 10 jobs failed,
for two causes.

## Cause 1: the installer job checked an empty environment

Fact (job 108416978854): the new step "A nightly pass runs on the installed vault"
ended in `ModuleNotFoundError: No module named 'yaml'`. The job sets
`UV_PROJECT_ENVIRONMENT` for every step; the installer ignores it and always builds
the vault's `.venv` (audit B-26), and the registered scheduler runs `uv run` without
it. So every step after the install ran in an environment nothing had synced. The
step before it (`repair_installed_memory.py --check`) passed only because it imports
no third-party package — it too was checking the wrong environment.

Fix: the job no longer sets `UV_PROJECT_ENVIRONMENT`; its steps read the `.venv` an
operator's install builds. Guard:
`tests/test_an_installed_vault_is_checked_in_its_own_environment.py` fails when any
job that runs `./install.sh` or `./install.ps1` points uv at another environment,
in its job or any step env (it fails on the previous workflow).

## Cause 2: the task check read a script variable

Fact (jobs on Linux, macOS and Windows): `test_windows_scheduler_status_accepts_only_the_registered_contract`
returned `[false, false]` for a correctly registered pair. The commit that moved the
hour limits into one table (`$LimitHours`) made `Test-LLMWikiScheduledTasks` read
that script-level variable; the test loads the functions alone, without the script
body, so the table was `$null` and every limit mismatched. The product runs the
whole script, where the table exists — so installs were not affected — but a
function that silently depends on its script's variables breaks for any caller that
loads it alone. This machine has no PowerShell on its PATH, so the test skipped here
and only CI ran it.

Fix: the table is a function, `Get-LLMWikiLimitHours`, which the check calls; the
script variable is set from it. The test loads that function with the others. Ran
here with a portable PowerShell 7: the test fails on the previous script and passes
now; 174 scheduler and installer tests pass.

## Sources

- uv, environment variables, fetched 2026-09-26,
  https://docs.astral.sh/uv/reference/environment/ — `UV_PROJECT_ENVIRONMENT`:
  "Specifies the path to the directory to use for a project virtual environment."
- GitHub Actions workflow syntax, fetched 2026-09-26,
  https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax —
  "an environment variable defined in a step will override job and workflow
  environment variables with the same name, while the step executes."
- PowerShell about_Scopes (7.6), fetched 2026-09-26,
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_scopes —
  "When a reference is made to a variable, alias, or function, PowerShell searches
  the current scope. If the item isn't found, the parent scope is searched."

Conclusion (mine): a job-level variable reaches every step, so the install and the
checks after it disagreed about the environment; and a function that finds its table
only by searching parent scopes works only where its script ran first.

## Files

- `.github/workflows/tests.yml`
- `scripts/install-scheduled-tasks.ps1`
- `tests/test_an_installed_vault_is_checked_in_its_own_environment.py`
- `tests/test_integration_injection.py`, `tests/test_the_scheduler_outlasts_the_pass.py`,
  `tests/test_a_changed_task_setting_reaches_an_installed_machine.py`
