"""The Windows installer's own small steps, run in PowerShell exactly as the installer defines them."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _powershell_functions, _pwsh

INSTALLER = Path(__file__).resolve().parents[1] / "install.ps1"
STEPS = ("Ok", "Warn", "Get-PowerShellHost", "Get-ForwardedSwitches", "Get-ReexecArguments",
         "Get-CodexMcpAdviceState", "Write-ModelAnswer")
NO_FLAGS = """
$ProtectPush = $false; $ConfirmAllAgentsStopped = $false; $ReplaceCodexMcp = $false
$CompileBackupImage = ""; $Model = ""; $Adopt = @()
"""
EVERY_FLAG = """
$ProtectPush = $true; $ConfirmAllAgentsStopped = $true; $ReplaceCodexMcp = $true
$CompileBackupImage = "D:\\image"; $Model = "claude/sonnet"; $Adopt = @("codex-hooks", "claude-settings")
"""


def _run(setup: str, expression: str) -> str:
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell is not installed; CI runs this on Windows")
    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / "steps.ps1"
        script.write_text(_powershell_functions(INSTALLER, STEPS) + setup + expression, encoding="utf-8")
        done = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    return done.stdout


def _reexec(setup: str) -> list[str]:
    return json.loads(_run(setup, "ConvertTo-Json -Compress -InputObject @(Get-ReexecArguments -Installer 'C:\\v\\install.ps1')"))


def test_a_clone_reruns_its_installer_with_no_flag_the_person_did_not_give() -> None:
    assert _reexec(NO_FLAGS) == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\v\\install.ps1"]


def test_a_clone_reruns_its_installer_with_every_flag_the_person_gave() -> None:
    assert _reexec(EVERY_FLAG) == [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\v\\install.ps1",
        "-ProtectPush", "-ConfirmAllAgentsStopped", "-ReplaceCodexMcp",
        "-CompileBackupImage", "D:\\image",
        "-Adopt", "codex-hooks", "-Adopt", "claude-settings",
        "-Model", "claude/sonnet",
    ]


def test_the_rerun_uses_the_powershell_that_runs_the_installer() -> None:
    expected = "Join-Path $PSHOME ($IsWindows ? 'pwsh.exe' : 'pwsh')"
    assert _run("", f"(Get-PowerShellHost) -eq ({expected})").strip() == "True"


@pytest.mark.parametrize(("exit_code", "state"), [(2, "foreign"), (1, "unverified")])
def test_codex_advice_names_the_entry_state_only_when_the_helper_reported_one(exit_code: int, state: str) -> None:
    said = _run("$script:codexMcpState = 'foreign'\n", f"Get-CodexMcpAdviceState -Exit {exit_code}")
    assert said.strip() == state


def test_the_person_is_told_which_model_was_saved_and_why_one_stayed() -> None:
    said = _run("", "Write-ModelAnswer ([pscustomobject]@{model='claude/sonnet'; reason='refused'}) 6>&1")
    assert said.splitlines() == ["[OK] Memory model saved in llm-wiki.toml: claude/sonnet",
                                 "[WARN] The memory model was not changed: refused"]
