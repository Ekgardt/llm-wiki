"""Helpers shared by test_a_changed_task_setting_reaches_an_installed_machine.py and the test files importing them."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


SCRIPT = ROOT / "scripts" / "install-scheduled-tasks.ps1"


def _pwsh() -> str:
    path = shutil.which("pwsh")
    if path is None:
        pytest.skip("PowerShell 7 is unavailable")
    return path


def _run(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


STUBS = """
$script:registered = @()
function Get-ScheduledTask { param($TaskName, $ErrorAction) $null }
function New-ScheduledTaskAction { param($Execute, $Argument) 'action' }
function New-ScheduledTaskTrigger { param([switch]$Daily, [switch]$Weekly, $DaysOfWeek, $At) 'trigger' }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) 'principal' }
function New-ScheduledTaskSettingsSet {
    param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable, $ExecutionTimeLimit, $RestartCount, $RestartInterval)
    $ExecutionTimeLimit.TotalHours
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description)
    $script:registered += ,@($Settings, $Description.EndsWith('[llm-wiki-task-spec:2]'))
}
"""
