"""Sqlite deadlines on caller-owned connections."""
from __future__ import annotations

import math
import sqlite3
import time


def operational_deadline_active(deadline: float | None) -> None:
    """Refuse an expired caller budget without creating runtime state."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("operational database deadline expired")

def operational_busy_ms(busy_ms: int, deadline: float | None) -> int:
    operational_deadline_active(deadline)
    if deadline is None or math.isinf(deadline):
        return busy_ms
    return min(busy_ms, max(0, int((deadline - time.monotonic()) * 1_000)))

# Cooperative SQL checks every 1 000 VM instructions. Measured 2026-10-03:
# a 100 000-row recursive aggregate took 0.384 s at interval 1 versus 0.0194 s
# here, with a 20 ms interruption overshoot below 16 microseconds. A retained
# authority fallback read was 0.153 s at 1 versus 0.0387 s here (same result).
# At 10 000, cost barely improved while overshoot grew; this is instrumentation
# granularity, never a row/data/work ceiling. Requalify on SQLite/workload changes;
# UDFs and filesystem/OS scheduling remain cooperative, not hard real-time.
_OPERATIONAL_PROGRESS_VM_STEPS = 1_000

def configure_operational_deadline(database: sqlite3.Connection, deadline: float | None) -> None:
    operational_deadline_active(deadline)
    if deadline is None or math.isinf(deadline):
        return

    def interrupted() -> int:
        return int(time.monotonic() >= deadline)

    database.set_progress_handler(interrupted, _OPERATIONAL_PROGRESS_VM_STEPS)

def refresh_operational_deadline(database: sqlite3.Connection, busy_ms: int, deadline: float | None) -> None:
    """Recompute lock waiting before the next transaction or commit."""
    operational_deadline_active(deadline)
    if deadline is None or math.isinf(deadline):
        return
    remaining_ms = operational_busy_ms(busy_ms, deadline)
    database.execute(f"PRAGMA busy_timeout={remaining_ms:d}")

# Preserve the historical public pickle/import identity without importing IO.
operational_deadline_active.__module__ = "reliable_memory"
operational_busy_ms.__module__ = "reliable_memory"
configure_operational_deadline.__module__ = "reliable_memory"
refresh_operational_deadline.__module__ = "reliable_memory"
