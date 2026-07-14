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

import memory_queue  # noqa: E402
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


def test_drain_heartbeats_long_handler_past_270_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = LockedClock()
    completed = threading.Event()
    waits: list[float] = []

    def wait(stop: threading.Event, interval: float) -> bool:
        waits.append(interval)
        if len(waits) <= 7:
            clock.advance(40)
            if len(waits) == 7:
                completed.set()
            return False
        return stop.wait(2)

    queue = MemoryQueue(
        tmp_path,
        clock=clock,
        rng=random.Random(1),
        heartbeat_wait=wait,
    )
    task_id = queue.enqueue("query", 1, {})
    heartbeat_calls: list[int] = []
    real_heartbeat = queue.heartbeat

    def heartbeat(lease, *, lease_seconds=120):
        heartbeat_calls.append(lease_seconds)
        return real_heartbeat(lease, lease_seconds=lease_seconds)

    monkeypatch.setattr(queue, "heartbeat", heartbeat)
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)

    counts = memory_queue.drain_with(
        lambda task: completed.wait(2),
        max_tasks=1,
    )
    assert counts == {"ok": 1, "failed": 0, "skipped": 0}
    assert heartbeat_calls == [120] * 7
    assert waits[:7] == [40] * 7
    assert queue.get(task_id).state == "succeeded"
    assert not any(
        thread.name == f"memory-queue-heartbeat-{task_id}" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_drain_reports_failure_when_heartbeat_loses_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = LockedClock()
    fence_lost = threading.Event()
    replacement: list[object] = []
    primary: MemoryQueue

    def wait(stop: threading.Event, interval: float) -> bool:
        del stop, interval
        clock.advance(121)
        other = MemoryQueue(tmp_path, clock=clock, rng=random.Random(3))
        replacement.append(other.claim("replacement"))
        return False

    primary = MemoryQueue(
        tmp_path,
        clock=clock,
        rng=random.Random(2),
        heartbeat_wait=wait,
    )
    task_id = primary.enqueue("query", 1, {})
    real_heartbeat = primary.heartbeat

    def heartbeat(lease, *, lease_seconds=120):
        try:
            return real_heartbeat(lease, lease_seconds=lease_seconds)
        except LeaseFenceError:
            fence_lost.set()
            raise

    monkeypatch.setattr(primary, "heartbeat", heartbeat)
    monkeypatch.setattr(memory_queue, "_queue", lambda: primary)

    counts = memory_queue.drain_with(
        lambda task: fence_lost.wait(2),
        max_tasks=1,
    )
    assert counts == {"ok": 0, "failed": 1, "skipped": 0}
    task = primary.get(task_id)
    assert replacement[0] is not None
    assert task.state == "leased"
    assert task.lease_owner == "replacement"
    assert task.result_reference is None
    assert not any(
        thread.name == f"memory-queue-heartbeat-{task_id}" and thread.is_alive()
        for thread in threading.enumerate()
    )
