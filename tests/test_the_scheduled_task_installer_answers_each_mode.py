"""The scheduler installer, run whole as a child process, answers each mode with the right exit code.

Task Scheduler is replaced by functions defined right after the script's own preamble,
so the script body, its dispatch and its `exit` run exactly as `install_windows` runs them.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _pwsh
from tests.powershell_literal import ps_literal

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-scheduled-tasks.ps1"
PREAMBLE = '$ErrorActionPreference = "Stop"\n'
SCHEDULER = """
$script:present = @({present})
function Get-ScheduledTask {{ param($TaskName, $ErrorAction)
    if ($script:present -contains $TaskName) {{ [pscustomobject]@{{ TaskName = $TaskName }} }} }}
function Unregister-ScheduledTask {{ param($TaskName, $Confirm, $ErrorAction) Write-Host "unregistered $TaskName" }}
function New-ScheduledTaskAction {{ param($Execute, $Argument) 'action' }}
function New-ScheduledTaskTrigger {{ param([switch]$Daily, [switch]$Weekly, $DaysOfWeek, $At) 'trigger' }}
function New-ScheduledTaskPrincipal {{ param($UserId, $LogonType, $RunLevel) 'principal' }}
function New-ScheduledTaskSettingsSet {{ param([switch]$AllowStartIfOnBatteries,
    [switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable, $ExecutionTimeLimit,
    $RestartCount, $RestartInterval) 'settings' }}
function Register-ScheduledTask {{ param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description)
    Write-Host "registered $TaskName" }}
function Start-ScheduledTask {{ param($TaskName) Write-Host "started $TaskName" }}
"""


def _machine(tmp_path: Path, present: tuple[str, ...]) -> tuple[str, Path]:
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell is not installed; CI runs this on Windows")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run-scheduled-task.ps1").write_text("", encoding="utf-8")
    (tmp_path / "uv.exe").write_text("", encoding="utf-8")
    stubs = SCHEDULER.format(present=", ".join(ps_literal(name) for name in present))
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count(PREAMBLE) == 1
    copy = tmp_path / "install-scheduled-tasks.ps1"
    copy.write_text(source.replace(PREAMBLE, PREAMBLE + stubs), encoding="utf-8")
    return executable, copy


def _roots(tmp_path: Path) -> list[str]:
    return ["-VaultRoot", str(tmp_path), "-StateRoot", str(tmp_path), "-UvPath", str(tmp_path / "uv.exe")]


def _run(tmp_path: Path, *flags: str, present: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    executable, copy = _machine(tmp_path, present)
    return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(copy), *_roots(tmp_path), *flags],
                          capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)


def test_state_json_reports_an_empty_machine_as_absent(tmp_path: Path) -> None:
    done = _run(tmp_path, "-StateJson")
    assert (done.returncode, json.loads(done.stdout.splitlines()[-1])) == (0, {"state": "absent"})


def test_status_fails_when_the_tasks_are_not_installed(tmp_path: Path) -> None:
    done = _run(tmp_path, "-Status")
    assert (done.returncode, "LLMWiki-Nightly: NOT INSTALLED" in done.stdout) == (1, True)


def test_uninstall_of_an_empty_machine_succeeds_without_touching_it(tmp_path: Path) -> None:
    done = _run(tmp_path, "-Uninstall")
    assert (done.returncode, "unregistered" in done.stdout) == (0, False)


@pytest.mark.parametrize(("flags", "refusal"), [(("-Uninstall",), "refusing uninstall"), ((), "refusing registration")])
def test_a_half_installed_machine_is_refused(tmp_path: Path, flags: tuple[str, ...], refusal: str) -> None:
    done = _run(tmp_path, *flags, present=("LLMWiki-Nightly",))
    assert (done.returncode, "registered" in done.stdout, refusal in done.stderr) == (1, False, True)


def test_install_registers_both_tasks_and_starts_only_the_requested_one(tmp_path: Path) -> None:
    done = _run(tmp_path, "-RunWeeklyNow")
    steps = [line for line in done.stdout.splitlines() if line.split(" ")[0] in {"registered", "started"}]
    assert (done.returncode, steps) == (0, ["registered LLMWiki-Nightly", "registered LLMWiki-Weekly",
                                            "started LLMWiki-Weekly"]), done.stderr


def test_dot_sourced_status_returns_the_verdict_instead_of_exiting(tmp_path: Path) -> None:
    executable, copy = _machine(tmp_path, ())
    roots = " ".join(ps_literal(value) if index % 2 else value for index, value in enumerate(_roots(tmp_path)))
    command = f"$verdict = @(. {ps_literal(str(copy))} {roots} -Status 6>$null); ConvertTo-Json -Compress $verdict"
    done = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert (done.returncode, json.loads(done.stdout.splitlines()[-1])) == (0, [False]), done.stderr
