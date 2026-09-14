"""One way to keep a held lease renewed: through a busy database, never past its expiry.

Seven renewers each decided alone when a failed renewal meant a lost lease — most
at the first exception, the doctor after two misses — and a database locked for a
few seconds ended nightly passes, writer gates, queue leases and backups. The rule
here is Kubernetes leader election's: renew on the interval, retry a transient
failure on a short period, and give up only when the lease itself has expired; a
failure that says the lease was taken is final at once.
See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable

# A failed renewal is retried this many times faster than the renewal interval.
RETRY_DIVISOR = 5


def busy_database(error: BaseException) -> bool:
    """A locked or busy SQLite database: the renewal may succeed if tried again."""
    return isinstance(error, sqlite3.OperationalError)


def _attempt(renew: Callable[[], object]) -> BaseException | None:
    try:
        renew()
    except Exception as exc:  # noqa: BLE001 - the caller decides what the error means
        return exc
    return None


class _Deadline:
    """When the lease runs out, measured from the start of its last good renewal."""

    def __init__(self, lease_seconds: float, monotonic: Callable[[], float]) -> None:
        self._lease_seconds = lease_seconds
        self._monotonic = monotonic
        self._expires = monotonic() + lease_seconds

    def renewed_at(self, started: float) -> None:
        self._expires = started + self._lease_seconds

    def remaining(self) -> float:
        return self._expires - self._monotonic()


def _retry_wait(error: BaseException, deadline: _Deadline, interval: float, transient) -> float | None:
    """How long to wait before retrying, or None when the lease is gone."""
    remaining = deadline.remaining()
    if not transient(error) or remaining <= 0:
        return None
    return min(interval / RETRY_DIVISOR, remaining)


def renew_until_stopped(
    renew: Callable[[], object],
    *,
    interval: float,
    lease_seconds: float,
    stop: threading.Event,
    transient: Callable[[BaseException], bool] = busy_database,
    wait: Callable[[float], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> BaseException | None:
    """Renew every `interval` until `stop`; the error that ended the lease, or None.

    `wait(seconds)` returns True when the renewer should stop; it defaults to
    `stop.wait`, and a queue passes its own injected seam.
    """
    waiter = stop.wait if wait is None else wait
    deadline = _Deadline(lease_seconds, monotonic)
    pause: float | None = interval
    while not waiter(pause):
        started = monotonic()
        error = _attempt(renew)
        if error is None:
            deadline.renewed_at(started)
            pause = interval
            continue
        pause = _retry_wait(error, deadline, interval, transient)
        if pause is None:
            return error
    return None
