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


SCRIPT = ROOT / "scripts" / "install-scheduled-tasks.ps1"
_TABLE = re.compile(r"^\$LimitHours = @\{ nightly = (\d+); weekly = (\d+) \}$", re.MULTILINE)


def script_limit_hours() -> dict[str, int]:
    """The one hours table the Windows script registers and checks its tasks by."""
    nightly, weekly = _TABLE.findall(SCRIPT.read_text(encoding="utf-8"))[0]
    return {"nightly": int(nightly), "weekly": int(weekly)}


def _nightly_limit_seconds() -> float:
    return float(script_limit_hours()["nightly"]) * 3600


def test_the_nightly_task_is_not_killed_inside_its_own_bounds(monkeypatch):
    """Measured in auto provider mode, the mode an installed scheduler runs in."""
    import scheduled_nightly

    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)

    assert scheduled_nightly.worst_case_seconds() < _nightly_limit_seconds()


def test_the_windows_script_says_the_hours_the_one_table_says():
    import install_control

    assert script_limit_hours() == install_control.SCHEDULER_LIMIT_HOURS


def test_the_windows_script_writes_its_hours_in_one_place():
    """A number next to `-Hours` or `LimitHours =` is a second copy that can drift (audit C-13)."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert re.findall(r"(?:-Hours|LimitHours =) \d", script) == []
