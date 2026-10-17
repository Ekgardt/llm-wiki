"""Memory failure is visible and cannot replace the wrapped process exit code."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_installer_bootstrap import _powershell_functions, _pwsh

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
