"""Clocks and locks a test controls, so a timing claim does not depend on the machine.

A test that reads the real clock and compares the difference with a fixed
margin passes or fails with the scheduler: on a hosted Windows runner the
monotonic clock moves in 15.6 ms steps, and run 45517849417 failed
`740.162 <= 740.046 + 0.11` with unchanged code. These stand in for the clock or
the lock the code under test reads, so the claim is checked exactly:

- `frozen_clock(now)` replaces a module's `time` with one whose clock never
  moves. A deadline the code computes is then exactly `now + budget`, and a call
  that waits for its deadline instead of refusing at once never returns -- which
  `slow_machine.run_within_hang_bound` turns into a failure.
- `VirtualClock` replaces a module's `time` with one whose `sleep` advances the
  clock instead of waiting, so a polling loop's total wait is measured exactly.
- `TimeoutRecordingLock` wraps a held lock and records every timeout a waiter
  hands it, so "the wait obeys the caller's deadline" is a comparison of numbers.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any


def frozen_clock(now: float) -> SimpleNamespace:
    """The `time` module, except that every clock reads `now` and never moves."""
    return SimpleNamespace(
        **{
            **vars(time),
            "monotonic": lambda: now,
            "perf_counter": lambda: now,
            "time": lambda: now,
        }
    )


class VirtualClock:
    """The `time` module, except that the clocks read `now` and `sleep` advances it."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class TimeoutRecordingLock:
    """A lock that hands every acquisition to `inner` and records its timeout."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.timeouts: list[float] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.timeouts.append(timeout)
        return bool(self.inner.acquire(blocking, timeout))

    def release(self) -> None:
        self.inner.release()

    def locked(self) -> bool:
        return bool(self.inner.locked())

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


class NeverSetEvent:
    """An event nobody sets: each wait records `(now, timeout)` and advances `clock`."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.waits: list[tuple[float, float]] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append((self.clock.now, timeout))
        self.clock.sleep(timeout)
        return False

    def is_set(self) -> bool:
        return False
