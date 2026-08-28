"""NEW-120: the claim reader accepts the heading this vault actually writes.

Measured 2026-08-28: every daily log here carries `# Daily Session Memory —
YYYY-MM-DD`, written by the single producer in `daily_log_append`, while
`_DATE_RE` required the bare `# YYYY-MM-DD` — a shape never written in this
vault. The subsystem had therefore never run: zero ledgers, no
`cache/claims.sqlite3`. Decision:
`knowledge/notes/daily-heading-tolerant-reader-decision.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from claims import _DATE_RE  # noqa: E402


def _matched(heading: str) -> str | None:
    match = _DATE_RE.match(heading)
    return match.group(1) if match else None


def test_the_titled_heading_this_vault_writes_is_accepted() -> None:
    assert _matched("# Daily Session Memory — 2026-08-28\n") == "2026-08-28"


def test_the_bare_heading_of_the_original_contract_still_works() -> None:
    assert _matched("# 2026-08-28\n") == "2026-08-28"


def test_a_heading_whose_date_does_not_end_it_is_refused() -> None:
    assert _matched("# Notes about 2026-08-28 and more\n") is None


def test_a_heading_without_a_date_is_refused() -> None:
    assert _matched("# knowledge/daily/\n") is None


def _daily_logs() -> list[Path]:
    daily = Path(__file__).resolve().parent.parent / "knowledge" / "daily"
    return sorted(path for path in daily.glob("*.md") if path.name != "README.md")


def _heading_of(path: Path) -> str:
    first = path.read_text(encoding="utf-8", errors="ignore").partition("\n")[0]
    return first + "\n"


def _refused_names(logs: list[Path]) -> list[str]:
    return [path.name for path in logs if _matched(_heading_of(path)) is None]


def test_every_daily_log_in_this_vault_is_readable() -> None:
    """The measure the decision names: no daily refused on its date."""
    logs = _daily_logs()
    assert logs, "the vault must hold daily logs for this to mean anything"
    assert _refused_names(logs) == []
