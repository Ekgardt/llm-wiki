# The CI and scheduler gaps

Dated 2026-09-14. Items 4.5 and 4.6 of `docs/AUDIT-2026-09-14-2.md`, decided on the
owner's delegation. The research before the change.

## What was found

- **PowerShell syntax.** The audit said `install.ps1` is not checked even
  syntactically. That is not quite right: `tests/test_integration_injection.py`
  parses `install.ps1` with `[System.Management.Automation.Language.Parser]::ParseFile`
  and throws on any parse error, and the Windows installer CI job runs those tests
  (`-k installer`). The other three scripts — `scripts/install-scheduled-tasks.ps1`
  (which registers the Windows tasks), `scripts/run-scheduled-task.ps1` and
  `scripts/codex-memory-wrapper.ps1` — are parsed by nothing; the regex test on the
  task limits reads text, not syntax.
- **Semantic and reranker tests.** Tests that need `torch` or `sentence-transformers`
  skip in every CI job; no job installs the `semantic` or `reranker` extras. Adding
  them means gigabytes of wheels and model weights per run.
- **systemd.** `install_control._systemd_service` renders `Type=oneshot` units with no
  `TimeoutStartSec`. For a oneshot service systemd's default start timeout is
  disabled, so a hung nightly pass holds its scheduled-owner lease with nothing to stop
  it — the case the Windows limits (3 h nightly, 5 h weekly) exist for. The pass's own
  bounds: `scheduled_nightly.worst_case_seconds()` 8 670 s,
  `scheduled_weekly.worst_case_seconds()` 14 190 s.
- **uv pinned exactly.** `pyproject.toml` `[tool.uv] required-version = "==0.12.3"`,
  the hooks and the CI all use 0.12.3 (`tests/test_dependency_contract.py` pins it). It
  is a reproducibility contract for `uv.lock`, not an accident.
- The code graph: `_systemd_service` ← `render_systemd_definitions` ←
  `systemd_scheduler_resource` (the Linux install path).

## Practice on this date

- `TimeoutStartSec=`: "Configures the time to wait for start-up… Defaults to
  DefaultTimeoutStartSec= … except for Type=oneshot services, where it is disabled by
  default" ([systemd.service(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#TimeoutStartSec=)).
- A parse check is the cheapest CI evidence that a script can run at all; PowerShell's
  own parser reports syntax errors without executing anything
  ([Parser.ParseFile](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parsefile)).

## The decision

- A test parses every tracked `*.ps1` with PowerShell's parser when `pwsh` (or
  `powershell`) is available, and skips otherwise; the full CI test shards run it on
  GitHub's Linux and Windows runners, which ship PowerShell.
- The systemd services get `TimeoutStartSec=` — 3 h for the nightly, 5 h for the weekly,
  the same limits as the Windows tasks — and a test keeps each above its pass's worst
  case.
- No semantic or reranker CI job in this change: the cost is gigabytes of wheels and
  model weights per run. The gap stays open and is recorded in the audit status.
- The exact uv pin stays.

Files: `scripts/install_control.py`,
`tests/test_ci_and_scheduler_gaps.py`,
`docs/research/2026-09-14-ci-and-scheduler-gaps.md`.
