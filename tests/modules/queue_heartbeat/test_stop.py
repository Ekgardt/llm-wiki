"""A heartbeat that does not stop is refused by name (audit OPS-18)."""

from __future__ import annotations

import threading

import pytest
from llm_wiki import queue_heartbeat

from tests.slow_machine import SHORT_TIMEOUT


class _DeafQueue:
    """A queue seam whose heartbeat wait never sees the stop event."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
        self.released.wait(SHORT_TIMEOUT)
        return True

    def heartbeat(self, lease, *, lease_seconds):
        return lease


def test_a_heartbeat_thread_that_ignores_stop_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(queue_heartbeat, "HEARTBEAT_STOP_FLOOR_SECONDS", 0.05)
    queue = _DeafQueue()
    lease = type("Lease", (), {"id": "lease-1"})()
    heartbeat = queue_heartbeat._LeaseHeartbeat(queue, lease, heartbeat_seconds=1, lease_seconds=2)
    heartbeat.start()
    try:
        with pytest.raises(queue_heartbeat.QueueOperationError, match="heartbeat_stop_timeout"):
            heartbeat.stop()
    finally:
        queue.released.set()
        heartbeat._thread.join(SHORT_TIMEOUT)
    assert not heartbeat._thread.is_alive()


def test_a_heartbeat_thread_that_stops_is_joined_quietly():
    class _Queue:
        def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
            return stop.wait(SHORT_TIMEOUT)

        def heartbeat(self, lease, *, lease_seconds):
            return lease

    lease = type("Lease", (), {"id": "lease-2"})()
    heartbeat = queue_heartbeat._LeaseHeartbeat(_Queue(), lease, heartbeat_seconds=1, lease_seconds=2)
    heartbeat.start()
    heartbeat.stop()
    assert not heartbeat._thread.is_alive()
