"""A worker whose lease is lost stops the child running the task instead of waiting it out.

The heartbeat's failure was read only after the child finished, while another worker
could already be running the same task. Research:
`docs/research/2026-09-14-a-lost-lease-stops-its-child.md`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import queue_core  # noqa: E402


def _long_processor(task: dict) -> bool:
    time.sleep(task["payload"]["seconds"])
    return True


def test_the_child_is_stopped_soon_after_the_lease_is_lost():
    started = time.monotonic()

    def lease_lost() -> bool:
        return time.monotonic() - started > 3.0

    with pytest.raises(queue_core.QueueOperationError, match="lease_lost"):
        queue_core.run_processor_child(_long_processor, {"payload": {"seconds": 120}}, 90.0, lease_lost=lease_lost)

    assert time.monotonic() - started < 30


def test_only_the_child_runner_is_made_interruptible():
    class Heartbeat:
        error = None

    injected = queue_core.run_processor_inline

    assert (
        queue_core.interruptible(injected, Heartbeat()) is injected,
        queue_core.interruptible(queue_core.run_processor_child, Heartbeat()).keywords.keys() == {"lease_lost", "cancelled"},
    ) == (True, True)
