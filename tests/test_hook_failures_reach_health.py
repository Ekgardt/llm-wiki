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
from datetime import timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from llm_wiki import doctor_capture  # noqa: E402

from tests.hook_failures_reach_health_helpers import NOW, _line, _trail  # noqa: E402


def test_a_failing_hook_is_reported_as_degraded(tmp_path: Path) -> None:
    _trail(tmp_path, [_line(NOW - timedelta(minutes=1))])

    finding = doctor_capture.hook_error_check(tmp_path, NOW)

    assert finding["status"] == "degraded"
    assert "still happening" in finding["message"]
    assert finding["details"]["kinds"] == {"project checkpoint": 1}


def test_an_old_trail_is_history_not_an_alarm(tmp_path: Path) -> None:
    """A finding describes a live condition; yesterday's failure is not one."""
    _trail(tmp_path, [_line(NOW - timedelta(days=2))])

    finding = doctor_capture.hook_error_check(tmp_path, NOW)

    assert finding["status"] == "ok"
    assert "none recently" in finding["message"]


def test_no_trail_is_not_a_finding(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()

    assert doctor_capture.hook_error_check(tmp_path, NOW)["status"] == "ok"


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

    details = doctor_capture.hook_error_check(tmp_path, NOW)["details"]

    assert details["kinds"] == {"project checkpoint": 2, "session end": 1}
    assert details["recent"] == 3


def test_an_unbounded_trail_cannot_stall_health(tmp_path: Path) -> None:
    """The window is fixed, so a log that grew for months still answers fast."""
    lines = [_line(NOW - timedelta(days=3)) for _ in range(20_000)]
    lines.append(_line(NOW - timedelta(minutes=1)))
    path = _trail(tmp_path, lines)
    assert path.stat().st_size > doctor_capture.HOOK_ERROR_TAIL_BYTES

    finding = doctor_capture.hook_error_check(tmp_path, NOW)

    assert finding["status"] == "degraded"
    assert finding["details"]["recent"] < len(lines)


def test_a_half_line_at_the_window_edge_is_not_a_record(tmp_path: Path) -> None:
    """A seek lands mid-line, and half a record must not be counted as one."""
    lines = [_line(NOW - timedelta(minutes=1)) for _ in range(2_000)]
    _trail(tmp_path, lines)

    records = doctor_capture.hook_error_records(
        doctor_capture.hook_error_lines(tmp_path / "logs" / "hook-errors.log")
    )

    assert all(kind == "project checkpoint" for _at, kind in records)


def test_an_unreadable_timestamp_is_not_read_as_live(tmp_path: Path) -> None:
    """A malformed line must not make a quiet system look like it is failing."""
    _trail(tmp_path, ["[not-a-timestamp] project checkpoint: ValueError: x"])

    assert doctor_capture.hook_error_check(tmp_path, NOW)["status"] == "ok"
