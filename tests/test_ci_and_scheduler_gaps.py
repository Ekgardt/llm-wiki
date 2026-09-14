"""Every PowerShell script parses, and a systemd pass is stopped above its own worst case.

A oneshot unit had no start timeout, and three PowerShell scripts were parsed by
nothing. Research: `docs/research/2026-09-14-ci-and-scheduler-gaps.md`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
UNIT_SECONDS = {"h": 3600, "min": 60, "s": 1}


def _tracked_powershell_scripts() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "*.ps1"], cwd=ROOT, capture_output=True, text=True, check=True)
    return listed.stdout.split()


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed here; the installer CI job has it")
@pytest.mark.parametrize("script", _tracked_powershell_scripts())
def test_every_powershell_script_parses(script):
    command = (
        "$tokens = $null; $errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile({json.dumps(str(ROOT / script))}, [ref]$tokens, [ref]$errors); "
        "if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
    )

    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr[-800:]


def _limit_seconds(value: str) -> int:
    unit = value.lstrip("0123456789")
    return int(value[: len(value) - len(unit)]) * UNIT_SECONDS[unit]


def test_each_systemd_pass_is_stopped_only_above_its_worst_case(tmp_path):
    import install_control
    import scheduled_nightly
    import scheduled_weekly

    definitions = install_control.render_systemd_definitions(tmp_path / "vault", tmp_path / "state", tmp_path / "uv")
    worst = {"nightly": scheduled_nightly.worst_case_seconds(), "weekly": scheduled_weekly.worst_case_seconds()}
    limits = {kind: install_control.SYSTEMD_START_LIMITS[kind] for kind in worst}
    rendered = all(f"TimeoutStartSec={limits[kind]}" in definitions[f"llm-wiki-{kind}.service"].decode() for kind in worst)

    assert (rendered, [kind for kind in worst if _limit_seconds(limits[kind]) <= worst[kind]]) == (True, [])
