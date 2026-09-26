# CI runs a reinstall and a pass

Date: 2026-09-26. Audit 2026-09-26, finding C-13 (no CI test of a reinstall, and
the scheduled passes never executed after an install).

## What was checked first (facts)

On 2026-09-26 on this Linux machine, in a sandbox that touched nothing of the
user's: a clone of this branch, a temporary HOME and state root, `MEMORY_LLM_PROVIDER=fake`,
a stand-in `crontab` that writes to a sandbox file, a stand-in `systemctl` that
reports no user manager (as on a hosted runner), and no agent CLI on PATH.

- `install.sh --scheduler cron`: exit 0 in 9 s (dependencies from the uv cache),
  Reliability v3 adopted, cron block written.
- The same command again over its own install: exit 0 in 5 s.
- `repair_installed_memory.py --check --json`: exit 0.
- `scheduled_nightly.py`, the command the cron block names: exit 0 in 212 s,
  `failures=0` (after the prune fix of
  `docs/research/2026-09-26-a-fresh-vault-has-nothing-to-prune.md`; before it the
  prune step failed on a fresh vault).

Not run here: macOS (bash 3.2, LaunchAgents) and Windows (install.ps1). Those run
only in CI; the new steps are the same commands the existing job already runs.

## Decision

The `install-end-to-end` job runs each installer a second time over its own
install before the check, and after the check runs one nightly pass on the
installed vault with the same environment. `tests/test_ci_runs_a_reinstall_and_a_pass.py`
fails if either step leaves the job.

Updates with untracked files already have their test
(`tests/test_an_untracked_copy_of_the_update_does_not_stop_it.py`, audit A-9).
Windows PowerShell 5.1 is not in the CI matrix and cannot run on Linux; the
changed PowerShell uses no 7-only syntax (parsed with the PowerShell 7.6.6 parser).

## Source

GitHub Docs, Workflow syntax for GitHub Actions,
https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax,
fetched 2026-09-26: `run` — "Use to run command-line programs using the operating
system's shell." / "Each `run` keyword represents a new process and shell in the
runner environment." `if` — "You can use the `if` conditional to prevent a step
from running unless a condition is met."

So each added step is a fresh process: the second install and the pass read only
what the first install left on disk, which is what an operator's reinstall and a
scheduler's start see.

## Files

- `.github/workflows/tests.yml`
- `tests/test_ci_runs_a_reinstall_and_a_pass.py`
- `tests/shard_weights.json`
