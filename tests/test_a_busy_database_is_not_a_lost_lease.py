"""A held lease survives a busy database until it expires, and a lost fence ends it at once.

Seven renewers gave up at the first or second failure; a 30-second lease was
declared lost at t=30 s behind one slow, locked renewal. Research:
`docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lease_renewal import renew_until_stopped  # noqa: E402


class _Clock:
    """Time that passes only when the renewer waits or a renewal takes it."""

    def __init__(self, stop_after: float) -> None:
        self.now = 0.0
        self.stop_after = stop_after
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(round(seconds, 3))
        self.now += seconds
        return self.now >= self.stop_after


class _Renewal:
    def __init__(self, clock: _Clock, outcomes: list[BaseException | None], cost: float = 0.0) -> None:
        self.clock = clock
        self.outcomes = outcomes
        self.cost = cost

    def __call__(self) -> None:
        self.clock.now += self.cost
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def _run(clock: _Clock, renewal: _Renewal, lease_seconds: float = 30.0):
    return renew_until_stopped(
        renewal,
        interval=10.0,
        lease_seconds=lease_seconds,
        stop=threading.Event(),
        wait=clock.wait,
        monotonic=clock.monotonic,
    )


def test_a_slow_locked_renewal_is_retried_before_the_lease_expires():
    """The measured case: one renewal blocked 10 s on a lock, then the lock lifts."""
    clock = _Clock(stop_after=60.0)
    locked = sqlite3.OperationalError("database is locked")
    renewal = _Renewal(clock, [None, locked, None])

    ended = _run(clock, renewal)

    assert ended is None
    assert clock.waits[:3] == [10.0, 10.0, 2.0]


def test_a_lock_that_outlasts_the_lease_ends_it():
    clock = _Clock(stop_after=600.0)
    locked = sqlite3.OperationalError("database is locked")

    ended = _run(clock, _Renewal(clock, [locked] * 100, cost=1.0))

    assert isinstance(ended, sqlite3.OperationalError)
    assert clock.now <= 40.0


def test_a_fence_taken_by_someone_else_ends_the_lease_at_once():
    clock = _Clock(stop_after=600.0)

    ended = _run(clock, _Renewal(clock, [RuntimeError("owner_fence_lost")]))

    assert (str(ended), clock.now) == ("owner_fence_lost", 10.0)
