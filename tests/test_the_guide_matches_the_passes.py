"""The user guide says what the passes do and the limits they run under; no hint drops extras.

The guide listed an FTS step that no longer exists, 3 h/5 h Windows limits (the
code has 4 h/6 h), and `uv sync --extra ...` hints, which uninstall every other
extra. See docs/research/2026-09-25-the-guide-matches-the-passes.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import install_control
import scheduled_weekly

ROOT = Path(__file__).resolve().parent.parent
GUIDE = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
EXACT_EXTRA_SYNC = re.compile(r"uv sync(?![^`\n]*--inexact)[^`\n]*--extra")


def test_the_guide_names_the_scheduler_limits_the_code_sets() -> None:
    hours = install_control.SCHEDULER_LIMIT_HOURS
    stated = f"{hours['nightly']} hours nightly and {hours['weekly']} hours"

    assert stated in " ".join(GUIDE.split())


def test_the_guide_names_every_weekly_step() -> None:
    words = {"okf": "OKF", "status": "queue status", "queue_purge": "purge finished queue work",
             "archive": "archive stale pages", "sessions": "archive session records",
             "daily_archive": "archive daily logs", "generations": "prune superseded evidence generations"}
    labels = [label for _message, label, _command, _timeout in scheduled_weekly._script_steps()]
    guide = " ".join(GUIDE.split())

    assert [label for label in labels if words.get(label, label) not in guide] == []


def test_no_hint_syncs_one_extra_exactly() -> None:
    sources = [*ROOT.glob("README*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / "scripts").glob("*.py")]
    found = [
        f"{path.name}: {match.group(0)}"
        for path in sources
        if not path.name.startswith("AUDIT-")
        for match in EXACT_EXTRA_SYNC.finditer(path.read_text(encoding="utf-8"))
    ]

    assert found == []
