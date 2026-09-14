"""The Windows nightly task's time limit sits above the pass's own worst case.

It was one hour against a pass bounded at about 2.4 hours. Research:
`docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _nightly_limit_seconds() -> float:
    script = (ROOT / "scripts" / "install-scheduled-tasks.ps1").read_text(encoding="utf-8")
    nightly = script.split("$nightlySettings", 1)[1].split("$nightlyPrincipal", 1)[0]
    hours = re.search(r"-ExecutionTimeLimit \(New-TimeSpan -Hours (\d+)\)", nightly)
    return float(hours.group(1)) * 3600


def test_the_nightly_task_is_not_killed_inside_its_own_bounds():
    import scheduled_nightly

    assert scheduled_nightly.worst_case_seconds() < _nightly_limit_seconds()
