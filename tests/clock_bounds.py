"""A remaining time read back from an absolute deadline, held to the budget it came from."""

from __future__ import annotations

import math
import time

# A clock reading just below 1024 s at which `(reading + 2.0) - reading` is
# 2.0000000000001137: a Windows runner hit such a reading (run 45482419969).
ROUNDING_CLOCK_READING = 1022.2687284882248


def within_budget(seconds: float, budget: float) -> bool:
    """Whether `seconds`, computed as `(start + budget) - now`, stays within `budget`.

    The sum `start + budget` rounds to its own float spacing, so the difference can
    exceed `budget` by up to one spacing of the deadline: near 1022 s of uptime,
    `(1022.2687284882248 + 2.0) - 1022.2687284882248` is 2.0000000000001137. The clock
    only grows, so the spacing of `now + budget` covers the deadline's; anything
    beyond it is a real overrun and fails.
    """
    return seconds <= budget + math.ulp(time.monotonic() + budget)
