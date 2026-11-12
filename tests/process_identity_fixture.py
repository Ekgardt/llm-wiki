"""Valid process identities for a simulated reused PID in our observation scope."""
from __future__ import annotations

import os

import process_liveness


def reused_current_process_identity() -> str:
    identity = process_liveness.process_start_identity(os.getpid())
    assert identity is not None
    prefix, ticks = identity.rsplit(":", 1)
    return f"{prefix}:{int(ticks) + 1}"
