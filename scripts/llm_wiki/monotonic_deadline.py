"""Whether an absolute monotonic deadline has passed, read through one patchable name."""
from __future__ import annotations

import time


def deadline_reached(deadline: float) -> bool:
    return time.monotonic() >= deadline
