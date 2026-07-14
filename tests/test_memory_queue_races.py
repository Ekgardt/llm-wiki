"""Concurrency and lease-loss tests for memory_queue."""

from __future__ import annotations

import random
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_queue import LeaseFenceError, MemoryQueue  # noqa: E402


class LockedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 7, 14, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: int) -> None:
        with self._lock:
            self._now += timedelta(seconds=seconds)


def test_concurrent_workers_claim_each_row_once_per_lease(tmp_path: Path) -> None:
    clock = LockedClock()
    seed = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    expected = {seed.enqueue("query", 1, {"n": number}) for number in range(40)}
    barrier = threading.Barrier(8)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(index + 10))
        barrier.wait()
        while lease := queue.claim(f"worker-{index}"):
            with lock:
                claimed.append(lease.id)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert set(claimed) == expected
    assert len(claimed) == len(expected)


def test_expired_lease_is_delivered_again_and_old_worker_is_fenced(tmp_path: Path) -> None:
    clock = LockedClock()
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("query", 1, {})
    first = queue.claim("first", lease_seconds=5)
    assert first is not None
    clock.advance(6)
    second = queue.claim("second", lease_seconds=5)
    assert second is not None and second.id == task_id
    assert second.token != first.token
    assert queue.get(task_id).attempts == 2
    assert queue.get(task_id).attempt_history[0].outcome == "lease_expired"

    with pytest.raises(LeaseFenceError):
        queue.publish_result(first, operation_id=task_id, result=b"stale")
