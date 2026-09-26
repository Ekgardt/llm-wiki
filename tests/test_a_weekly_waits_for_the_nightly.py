"""A weekly that meets the nightly waits for it, and one that never ran is named (audit 2026-09-26 B-20).

docs/research/2026-09-26-a-weekly-waits-for-the-nightly.md
"""
from __future__ import annotations

from datetime import datetime, timezone

import doctor
import install_control
import scheduled_weekly


def test_the_weekly_takes_the_fence_once_the_nightly_lets_go(monkeypatch) -> None:
    answers = iter((None, None, "fence"))
    slept: list[float] = []
    monkeypatch.setattr(scheduled_weekly, "_fence_unless_busy", lambda: next(answers))

    fence = scheduled_weekly._fence_after_waiting(sleep=slept.append, clock=lambda: 0.0)

    assert (fence, slept) == ("fence", [60.0, 60.0])


def test_the_wait_fits_under_the_scheduler_limit() -> None:
    assert scheduled_weekly.worst_case_seconds() < install_control.SCHEDULER_LIMIT_HOURS["weekly"] * 3600


def test_a_weekly_that_only_ever_skipped_is_named() -> None:
    state = {"last_weekly_skip": {"skipped_at": "2026-09-20T03:00:00+00:00", "reason": "owner_busy"}}

    verdict = doctor._weekly_verdict(state, datetime.now(timezone.utc))

    assert verdict is not None and verdict[0] == "degraded"
