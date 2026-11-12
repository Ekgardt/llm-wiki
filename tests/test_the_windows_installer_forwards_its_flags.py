"""The Windows installer's own small steps, run in PowerShell exactly as the installer defines them."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _powershell_functions, _pwsh

INSTALLER = Path(__file__).resolve().parents[1] / "install.ps1"
STEPS = ("Ok", "Warn", "Fail", "Get-PowerShellHost", "Get-ForwardedSwitches", "Get-ReexecArguments",
         "Get-CodexMcpAdviceState", "Write-ModelAnswer", "Get-BootstrapPython", "Get-ChooserArguments",
         "Select-MemoryModel", "Repair-ManagedPyright")
UV = """
$global:uvCalls = @()
function uv { $global:uvCalls += ,@($args); $script:uvSays; $global:LASTEXITCODE = $script:uvExit }
"""
NO_FLAGS = """
$ProtectPush = $false; $ConfirmAllAgentsStopped = $false; $ReplaceCodexMcp = $false
$CompileBackupImage = ""; $Model = ""; $Adopt = @()
"""
EVERY_FLAG = """
$ProtectPush = $true; $ConfirmAllAgentsStopped = $true; $ReplaceCodexMcp = $true
$CompileBackupImage = "D:\\image"; $Model = "claude/sonnet"; $Adopt = @("codex-hooks", "claude-settings")
"""


def _process(setup: str, expression: str) -> subprocess.CompletedProcess[str]:
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell is not installed; CI runs this on Windows")
    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / "steps.ps1"
        script.write_text(_powershell_functions(INSTALLER, STEPS) + setup + expression, encoding="utf-8")
        return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)


def _run(setup: str, expression: str) -> str:
    done = _process(setup, expression)
    assert done.returncode == 0, done.stderr
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


def _uv(says: str, exit_code: int) -> str:
    return UV + f"$script:uvSays = '{says}'; $script:uvExit = {exit_code}\n"


def test_python_314_is_found_through_uv() -> None:
    said = _run(_uv("/opt/python3.14", 0), "Get-BootstrapPython; $global:uvCalls[0] -join ' '")
    assert said.splitlines() == ["/opt/python3.14", "python find 3.14"]


def test_a_missing_python_314_stops_the_installer() -> None:
    done = _process(_uv("", 2), "Get-BootstrapPython; 'continued'")
    assert (done.returncode, "[FAIL] Python 3.14 is required" in done.stdout, "continued" in done.stdout) == (
        1, True, False)


@pytest.mark.parametrize(("model", "tail"), [("", ["--home", "C:\\Users\\me"]),
                                             ("claude/sonnet", ["--model", "claude/sonnet"])])
def test_the_model_chooser_is_asked_for_the_model_the_person_named(model: str, tail: list[str]) -> None:
    setup = f"$VAULT_ROOT = 'C:\\v'; $env:USERPROFILE = 'C:\\Users\\me'; $Model = '{model}'\n"
    arguments = json.loads(_run(setup, "ConvertTo-Json -Compress -InputObject @(Get-ChooserArguments)"))
    assert (arguments[:5], arguments[-2:]) == (["run", "--locked", "--no-sync", "--directory", "C:\\v"], tail)


def test_the_chosen_model_is_reported() -> None:
    said = _run(_uv('{"model": "claude/sonnet"}', 0) + "$VAULT_ROOT = 'C:\\v'; $Model = ''\n", "Select-MemoryModel 6>&1")
    assert said.strip() == "[OK] Memory model saved in llm-wiki.toml: claude/sonnet"


def test_a_failed_model_choice_stops_the_installer() -> None:
    done = _process(_uv("", 1) + "$VAULT_ROOT = 'C:\\v'; $Model = ''\n", "Select-MemoryModel; 'continued'")
    assert (done.returncode, "[FAIL] Model choice failed" in done.stdout, "continued" in done.stdout) == (1, True, False)


@pytest.mark.parametrize(("installed", "exit_code", "said"), [
    (False, 0, ""), (True, 0, "[OK] Managed Pyright verified"),
    (True, 1, "[WARN] Managed Pyright could not be verified or repaired: digest mismatch")])
def test_a_managed_pyright_is_checked_only_when_installed(tmp_path: Path, installed: bool, exit_code: int,
                                                          said: str) -> None:
    if installed:
        (tmp_path / "cache" / "code-tools" / "pyright").mkdir(parents=True)
    setup = _uv("digest mismatch", exit_code) + f"$STATE_ROOT = '{tmp_path}'; $VAULT_ROOT = 'C:\\v'\n"
    output = _run(setup, "Repair-ManagedPyright 6>&1; 'calls=' + $global:uvCalls.Count")
    assert output.splitlines() == [line for line in (said, f"calls={int(installed)}") if line]
