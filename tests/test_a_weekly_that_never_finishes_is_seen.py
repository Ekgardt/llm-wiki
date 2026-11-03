"""A weekly that never ran or never finished is visible by its age (audit 2026-09-27 B-15).

docs/research/2026-09-27-a-weekly-that-never-finishes-is-seen.md
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import doctor

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _stamp(delta: timedelta) -> str:
    return (NOW - delta).isoformat(timespec="seconds")


def test_a_weekly_due_for_over_a_period_with_no_run_is_named() -> None:
    state = {"weekly_due_since": _stamp(timedelta(days=9))}

    assert doctor._weekly_lateness(state, NOW) == "Weekly maintenance has never completed."


def test_a_weekly_that_started_and_left_no_result_is_named() -> None:
    state = {
        "last_weekly_at": _stamp(timedelta(days=6)),
        "last_weekly_started_at": _stamp(timedelta(hours=7)),
    }

    assert "did not finish" in str(doctor._weekly_lateness(state, NOW))


def test_a_weekly_that_finished_after_it_started_is_quiet() -> None:
    state = {
        "weekly_due_since": _stamp(timedelta(days=30)),
        "last_weekly_started_at": _stamp(timedelta(hours=7)),
        "last_weekly_at": _stamp(timedelta(hours=5)),
    }

    assert doctor._weekly_lateness(state, NOW) is None
