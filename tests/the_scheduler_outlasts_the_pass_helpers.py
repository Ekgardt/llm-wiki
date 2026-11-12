"""Helpers shared by test_the_scheduler_outlasts_the_pass.py and the test files importing them."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


SCRIPT = ROOT / "scripts" / "install-scheduled-tasks.ps1"


_TABLE = re.compile(r"^function Get-LLMWikiLimitHours \{ return @\{ nightly = (\d+); weekly = (\d+) \} \}$", re.MULTILINE)


def script_limit_hours() -> dict[str, int]:
    """The one hours table the Windows script registers and checks its tasks by."""
    nightly, weekly = _TABLE.findall(SCRIPT.read_text(encoding="utf-8"))[0]
    return {"nightly": int(nightly), "weekly": int(weekly)}
