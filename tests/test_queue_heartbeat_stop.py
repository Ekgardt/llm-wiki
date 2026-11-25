"""A heartbeat that does not stop is refused by name (audit OPS-18)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import memory_queue  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


class _DeafQueue:
    """A queue seam whose heartbeat wait never sees the stop event."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
        self.released.wait(SHORT_TIMEOUT)
        return True

    def heartbeat(self, lease, *, lease_seconds):
        return lease


def test_a_heartbeat_thread_that_ignores_stop_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_queue, "HEARTBEAT_STOP_FLOOR_SECONDS", 0.05)
    queue = _DeafQueue()
    lease = type("Lease", (), {"id": "lease-1"})()
    heartbeat = memory_queue._LeaseHeartbeat(queue, lease, heartbeat_seconds=1, lease_seconds=2)
    heartbeat.start()
    try:
        with pytest.raises(memory_queue.QueueOperationError, match="heartbeat_stop_timeout"):
            heartbeat.stop()
    finally:
        queue.released.set()
        heartbeat._thread.join(SHORT_TIMEOUT)
    assert not heartbeat._thread.is_alive()


def test_the_refusal_keeps_its_bound_when_the_interpreter_join_ignores_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPython 3.13 can turn a timed join into an untimed one (python/cpython#161712).

    A hosted Windows runner did: the stop waited 30 s for the deaf heartbeat to end
    and then raised nothing (run 45638724158). The emulated join below waits for the
    thread's end whatever timeout it is given.
    """
    real_join = threading.Thread.join

    def join_ignoring_its_timeout(thread: threading.Thread, timeout: float | None = None) -> None:
        real_join(thread)

    monkeypatch.setattr(threading.Thread, "join", join_ignoring_its_timeout)
    test_a_heartbeat_thread_that_ignores_stop_is_refused_by_name(monkeypatch)


def test_a_heartbeat_thread_that_stops_is_joined_quietly():
    class _Queue:
        def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
            return stop.wait(SHORT_TIMEOUT)

        def heartbeat(self, lease, *, lease_seconds):
            return lease

    lease = type("Lease", (), {"id": "lease-2"})()
    heartbeat = memory_queue._LeaseHeartbeat(_Queue(), lease, heartbeat_seconds=1, lease_seconds=2)
    heartbeat.start()
    heartbeat.stop()
    assert not heartbeat._thread.is_alive()
