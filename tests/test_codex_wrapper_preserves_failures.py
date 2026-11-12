"""Memory failure is visible and cannot replace the wrapped process exit code."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _powershell_functions, _pwsh

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("no_memory", [False, True])
def test_capture_failure_warns_and_keeps_codex_exit(no_memory):
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell unavailable")
    source = _powershell_functions(
        ROOT / "scripts/codex-memory-wrapper.ps1", ("codex", "Complete-CodexMemorySession"),
    )
    command = source + """
function Initialize-CodexMemoryContext { param($Directory) }
function Save-CodexMemorySession { param($Directory, $Reason); throw 'injected capture failure' }
$REAL_CODEX = { $global:LASTEXITCODE = 7 }
""" + f"""
$warnings = @()
codex -NoMemory:${str(no_memory).lower()} -WarningVariable warnings 3>$null
@{{ exit = $LASTEXITCODE; warnings = @($warnings | ForEach-Object {{ [string]$_ }}) }} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=True,
    )
    record = json.loads(result.stdout)
    assert record["exit"] == 7
    assert bool(record["warnings"]) is not no_memory
    assert no_memory or "injected capture failure" in record["warnings"][0]


LOOKUP = ("Find-CodexOnWindows", "Find-CodexOnPath", "Find-RealCodex", "Test-RealCodex")


def _lookup(tmp_path: Path, shims: tuple[str, ...], expression: str) -> str:
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell unavailable")
    npm = tmp_path / "npm"
    npm.mkdir()
    for shim in shims:
        (npm / shim).write_text("", encoding="utf-8")
    script = tmp_path / "lookup.ps1"
    script.write_text(_powershell_functions(ROOT / "scripts/codex-memory-wrapper.ps1", LOOKUP)
                      + f"\n$env:APPDATA = '{tmp_path}'\n{expression}\n", encoding="utf-8")
    done = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    return done.stdout.strip()


@pytest.mark.parametrize(("shims", "chosen"), [
    (("codex.cmd", "codex.ps1"), "codex.cmd"), (("codex.ps1",), "codex.ps1"), ((), "")])
def test_windows_lookup_prefers_the_npm_shims_in_order(tmp_path, shims, chosen):
    found = _lookup(tmp_path, shims, "function Get-Command { param($Name, $ErrorAction) }\n"
                    "[string](Find-CodexOnWindows)")
    assert found == "".join(f"{tmp_path}\\npm\\{name}" for name in shims[:1])
    assert found.endswith(chosen)


def test_path_lookup_takes_only_an_application_never_the_wrapper_function(tmp_path):
    found = _lookup(tmp_path, (), """
function Get-Command { param($Name, [switch]$All, $ErrorAction)
    [pscustomobject]@{ CommandType = 'Function'; Source = 'wrapper' }
    [pscustomobject]@{ CommandType = 'Application'; Source = '/usr/bin/codex' } }
[string](Find-CodexOnPath)""")
    assert found == "/usr/bin/codex"


@pytest.mark.parametrize(("path", "usable"), [("", "False"), ("missing", "False"), ("npm", "True")])
def test_only_an_existing_binary_enables_the_wrapper(tmp_path, path, usable):
    target = str(tmp_path / path) if path else ""
    assert _lookup(tmp_path, (), f"Test-RealCodex -Path '{target}'") == usable
