"""The installer confirms "all agents stopped" only where it can see that it is true.

A fresh vault holds no legacy database, so there the statement is true by inspection. A vault
that holds the earlier queue is adopted only when the operator said so to the installer.

Research: `docs/research/2026-09-17-the-installer-does-not-vouch-for-agents-it-cannot-see.md`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from slow_machine import LONG_TIMEOUT  # noqa: E402
from test_installer_bootstrap import (  # noqa: E402
    _bash,
    _powershell_functions,
    _pwsh,
    _shell_function,
)

ROOT = TESTS.parent
INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
INSTALL_PS1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
needs_bash = pytest.mark.skipif(_bash() is None, reason="bash is not installed")
needs_pwsh = pytest.mark.skipif(_pwsh() is None, reason="PowerShell is not installed")

# (state, the operator confirmed) -> what the installer does
PLAN = [
    ("adopted", 0, "adopted"),
    ("fresh", 0, "adopt"),
    ("upgrade-required", 0, "ask"),
    ("upgrade-required", 1, "adopt"),
    ("partial", 0, "ask"),
    ("partial", 1, "adopt"),
    ("conflict", 1, "unknown"),
    ("unknown", 0, "unknown"),
]


@needs_bash
@pytest.mark.parametrize(("state", "confirmed", "expected"), PLAN)
def test_only_a_fresh_vault_is_adopted_without_the_operator(state, confirmed, expected) -> None:
    script = f"set -euo pipefail\n{_shell_function(INSTALL_SH, 'adoption_plan')}\nadoption_plan \"$@\"\n"

    result = subprocess.run(
        [_bash(), "-c", script, "adoption_plan", state, str(confirmed)],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )

    assert (result.returncode, result.stdout.strip()) == (0, expected)


@needs_pwsh
def test_the_windows_installer_plans_the_same() -> None:
    calls = "\n".join(
        f"$plans += Get-AdoptionPlan -State {json.dumps(state)} -Confirmed ${bool(confirmed)}"
        for state, confirmed, _expected in PLAN
    )
    command = _powershell_functions(ROOT / "install.ps1", ("Get-AdoptionPlan",)) + (
        f"$plans = @()\n{calls}\nConvertTo-Json -Compress $plans\n"
    )

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [expected for _s, _c, expected in PLAN]


def _code(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


@pytest.mark.parametrize(
    ("text", "flag"),
    [(INSTALL_SH, "--confirm-all-agents-stopped) AGENTS_STOPPED=1"), (INSTALL_PS1, "[switch]$ConfirmAllAgentsStopped")],
    ids=["install.sh", "install.ps1"],
)
def test_the_confirmation_is_an_argument_of_the_installer_and_is_run_once(text: str, flag: str) -> None:
    """One place runs the apply command, and it sits behind the plan."""
    applies = [line for line in _code(text) if "--apply --adopt-ownership-v3" in line and "repair_installed_memory.py\"" in line]

    assert (flag in text, len(applies)) == (True, 1)
