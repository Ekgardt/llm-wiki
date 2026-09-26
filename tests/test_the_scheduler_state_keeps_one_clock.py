"""The scheduler state writes every instant in UTC, and doctor compares instants, not strings.

`skipped_at` was local and `last_nightly_at` UTC, compared as text. See
docs/research/2026-09-25-the-scheduler-state-keeps-one-clock.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import doctor
import scheduled_nightly


def test_every_written_instant_carries_its_offset() -> None:
    assert datetime.fromisoformat(scheduled_nightly._utc_now()).utcoffset() == timedelta(0)


def test_a_skip_is_compared_with_the_run_as_instants() -> None:
    ran = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)
    skipped_before = (ran - timedelta(hours=1)).astimezone(timezone(timedelta(hours=-5)))
    skipped_after = (ran + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=9)))
    state = {"last_nightly_at": ran.isoformat()}

    assert (
        doctor._skip_is_newer_than_run(state, {"skipped_at": skipped_before.isoformat()}),
        doctor._skip_is_newer_than_run(state, {"skipped_at": skipped_after.isoformat()}),
    ) == (False, True)
