"""Run, operation and cleanup deadlines the Python navigation qualification shares."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

QUERY_TIMEOUT_SECONDS = 90.0
CLEANUP_TIMEOUT_SECONDS = 30.0


class BenchmarkTimeoutError(TimeoutError):
    """Raised when the one absolute benchmark run budget is exhausted."""


def _check_run_deadline(
    run_deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    now = monotonic()
    if not math.isfinite(run_deadline) or now >= run_deadline:
        raise BenchmarkTimeoutError("benchmark run deadline exceeded")
    return now


def _operation_deadline(
    run_deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    limit: float = QUERY_TIMEOUT_SECONDS,
) -> float:
    now = _check_run_deadline(run_deadline, monotonic=monotonic)
    return min(run_deadline, now + limit)


def _fresh_cleanup_deadline(
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    return monotonic() + CLEANUP_TIMEOUT_SECONDS
