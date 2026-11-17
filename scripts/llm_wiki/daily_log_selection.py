"""Select daily-log filenames from an explicitly supplied directory."""
from __future__ import annotations

import re
from pathlib import Path

# `knowledge/daily/` also holds a tracked `README.md`. It is not a daily log:
# it has no date and is never compiled. Every reader of that directory asks
# here, so the rule has one home and no reader can forget it.
# Research: docs/research/2026-09-17-a-daily-log-is-named-by-its-date-everywhere.md
DAILY_LOG_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.md")


def daily_logs(daily_dir: Path) -> list[Path]:
    """The `YYYY-MM-DD.md` files of a daily directory, oldest first; none if it is absent."""
    if not daily_dir.is_dir():
        return []
    return sorted(
        path for path in daily_dir.glob("*.md") if DAILY_LOG_NAME.fullmatch(path.name) is not None
    )


def closed_daily_logs(daily_dir: Path) -> list[Path]:
    """Every daily log but the newest: the newest is the one still being appended to.

    Compiling it at every session start sent the whole open day again after each
    append, padded to the compile window each time (2026-09-11: 36 KB of day text in
    six batches for a 10.6 KB day). Like a log's active segment, it is compiled when
    it closes; the nightly and a manual run still take every day. See
    `docs/research/2026-09-27-an-estimate-is-measured-and-an-open-day-waits.md`.
    """
    return daily_logs(daily_dir)[:-1]
