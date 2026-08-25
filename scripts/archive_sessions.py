"""Move session records out of the active tree once they are old enough.

Every session leaves a redacted copy of itself under
`knowledge/raw/sessions/<date>/`. Those records are never deleted — they are the
evidence a compiled page is built from — but they need not stay in the active
tree forever: this vault writes about a megabyte of them a day, and the
consolidation pass only ever reads yesterday.

A record older than the retention window moves to
`knowledge/raw/sessions/archive/<YYYY-MM>/<date>/`. Nothing is removed, the
bytes are unchanged, and `grep` still finds them one directory deeper. The move
goes through the same transaction machinery as every other automatic writer, so
an interrupted archive is recoverable rather than a half-moved day.

Research: `docs/research/2026-08-25-when-a-session-record-leaves-the-hot-tree.md`.

Usage:
    uv run python scripts/archive_sessions.py             # dry-run (plan only)
    uv run python scripts/archive_sessions.py --apply
    uv run python scripts/archive_sessions.py --days 30
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import ABSENT, mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

# How long a record stays in the active tree: the same ninety days the archive
# contract already gives every other hot artifact. The observability write-ups
# favour thirty, but consolidation reads yesterday while a person looking for
# how something was decided reaches back weeks.
DEFAULT_RETENTION_DAYS = 90

# One record is a redacted transcript; this vault's largest is a third of a
# megabyte, and the bound refuses anything that is no longer a session record.
MAX_RECORD_BYTES = 8 * 1024 * 1024

SESSIONS = ROOT / "knowledge" / "raw" / "sessions"
ARCHIVE = SESSIONS / "archive"
_DAY_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _day_of(directory: Path) -> date | None:
    match = _DAY_NAME.fullmatch(directory.name)
    if match is None:
        return None
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _day_directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(item for item in root.iterdir() if item.is_dir() and _day_of(item))


def aged_out(root: Path, cutoff: date) -> list[Path]:
    """Every day directory whose date is older than the cutoff."""
    return [day for day in _day_directories(root) if (_day_of(day) or cutoff) < cutoff]


def _destination(record: Path, day: Path) -> Path:
    return ARCHIVE / day.name[:7] / day.name / record.name


def _records(day: Path) -> list[Path]:
    return sorted(item for item in day.iterdir() if item.is_file())


def _moved(record: Path, destination: Path) -> str:
    relative = record.relative_to(ROOT).as_posix()
    content = read_stable_bytes(record, MAX_RECORD_BYTES, label="session record")
    mutate_knowledge(
        stable_operation_id("archive-session", relative, content),
        {destination: content, record: None},
        preconditions={
            relative: sha256_bytes(content),
            destination.relative_to(ROOT).as_posix(): ABSENT,
        },
    )
    return f"ARCHIVED: {relative}"


def _attempt(record: Path, day: Path) -> str:
    try:
        return _moved(record, _destination(record, day))
    except (OSError, RuntimeError, ValueError) as exc:
        return f"ERROR: {record.relative_to(ROOT).as_posix()}: {type(exc).__name__}"


def _archive_day(day: Path) -> list[str]:
    """Archive one day; the empty directory goes only when every record moved."""
    outcomes = [_attempt(record, day) for record in _records(day)]
    if all(outcome.startswith("ARCHIVED:") for outcome in outcomes):
        day.rmdir()
    return outcomes


def _planned(stale: list[Path]) -> list[str]:
    return [
        f"WOULD ARCHIVE: {record.relative_to(ROOT).as_posix()}"
        for day in stale
        for record in _records(day)
    ]


def _archived(stale: list[Path]) -> list[str]:
    return [outcome for day in stale for outcome in _archive_day(day)]


def archive_sessions(*, days: int, today: date, apply: bool) -> list[str]:
    """Archive every record older than the window; a dry run only names them."""
    stale = aged_out(SESSIONS, today - timedelta(days=days))
    if not apply:
        return _planned(stale)
    return _archived(stale)


def _report(outcomes: list[str]) -> int:
    for line in outcomes:
        print(f"  {line}")
    failures = len([line for line in outcomes if line.startswith("ERROR:")])
    print(f"archive_sessions: {len(outcomes)} record(s), {failures} failed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--apply", action="store_true", help="move the records")
    arguments = parser.parse_args(argv)
    if arguments.days < 1:
        parser.error("--days must be at least one day")
    return _report(
        archive_sessions(
            days=arguments.days, today=date.today(), apply=arguments.apply
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
