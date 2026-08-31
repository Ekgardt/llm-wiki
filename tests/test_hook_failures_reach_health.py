"""The hook failure trail was written by the product and read by nobody.

Every lifecycle hook that fails appends one bounded line to
`logs/hook-errors.log` and returns, because capture must never break a session.
No health check ever opened that file, so a failing hook was invisible.

Measured on the live vault 2026-08-29: 5 682 failures between 2026-08-25T09:32
and 2026-08-29T21:04 — project checkpointing failing every ten seconds for five
days — with doctor reporting nothing about it. In the meantime the queue those
checkpoints feed reached 4 643 undrained events and `run/state.json` reached
10 MB. What made the outage long was not the defect; it was that nothing said
so.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402

NOW = datetime(2026, 8, 29, 21, 10, tzinfo=timezone.utc)


def _trail(state_root: Path, lines: list[str]) -> Path:
    logs = state_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "hook-errors.log"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _line(at: datetime, kind: str = "project checkpoint") -> str:
    return f"[{at.isoformat()}] {kind}: ValueError: something went wrong"


def test_a_failing_hook_is_reported_as_degraded(tmp_path: Path) -> None:
    _trail(tmp_path, [_line(NOW - timedelta(minutes=1))])

    finding = doctor._hook_error_check(tmp_path, NOW)

    assert finding["status"] == "degraded"
    assert "still happening" in finding["message"]
    assert finding["details"]["kinds"] == {"project checkpoint": 1}


def test_an_old_trail_is_history_not_an_alarm(tmp_path: Path) -> None:
    """A finding describes a live condition; yesterday's failure is not one."""
    _trail(tmp_path, [_line(NOW - timedelta(days=2))])

    finding = doctor._hook_error_check(tmp_path, NOW)

    assert finding["status"] == "ok"
    assert "none recently" in finding["message"]


def test_no_trail_is_not_a_finding(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()

    assert doctor._hook_error_check(tmp_path, NOW)["status"] == "ok"


def test_each_kind_of_failure_is_counted_separately(tmp_path: Path) -> None:
    """Which hook is failing is the first thing an operator needs."""
    _trail(
        tmp_path,
        [
            _line(NOW - timedelta(minutes=2), "project checkpoint"),
            _line(NOW - timedelta(minutes=2), "project checkpoint"),
            _line(NOW - timedelta(minutes=1), "session end"),
        ],
    )

    details = doctor._hook_error_check(tmp_path, NOW)["details"]

    assert details["kinds"] == {"project checkpoint": 2, "session end": 1}
    assert details["recent"] == 3


def test_an_unbounded_trail_cannot_stall_health(tmp_path: Path) -> None:
    """The window is fixed, so a log that grew for months still answers fast."""
    lines = [_line(NOW - timedelta(days=3)) for _ in range(20_000)]
    lines.append(_line(NOW - timedelta(minutes=1)))
    path = _trail(tmp_path, lines)
    assert path.stat().st_size > doctor.HOOK_ERROR_TAIL_BYTES

    finding = doctor._hook_error_check(tmp_path, NOW)

    assert finding["status"] == "degraded"
    assert finding["details"]["recent"] < len(lines)


def test_a_half_line_at_the_window_edge_is_not_a_record(tmp_path: Path) -> None:
    """A seek lands mid-line, and half a record must not be counted as one."""
    lines = [_line(NOW - timedelta(minutes=1)) for _ in range(2_000)]
    _trail(tmp_path, lines)

    records = doctor._hook_error_records(
        doctor._hook_error_lines(tmp_path / "logs" / "hook-errors.log")
    )

    assert all(kind == "project checkpoint" for _at, kind in records)


def test_an_unreadable_timestamp_is_not_read_as_live(tmp_path: Path) -> None:
    """A malformed line must not make a quiet system look like it is failing."""
    _trail(tmp_path, ["[not-a-timestamp] project checkpoint: ValueError: x"])

    assert doctor._hook_error_check(tmp_path, NOW)["status"] == "ok"
