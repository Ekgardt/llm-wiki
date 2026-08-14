"""Concurrency and lease-loss tests for memory_queue."""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
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


def _v3_queue(tmp_path: Path):
    coordinator = tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    queue_path = tmp_path / "run/queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    memory_queue.initialize_queue_v3_candidate(queue_path, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(queue_path, state_root=tmp_path)


def test_queue_projection_failure_releases_only_the_exact_canonical_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _v3_queue(tmp_path)
    successor: list[operational_ownership.OwnerLease] = []
    real_release = operational_ownership.OwnershipRegistry.release

    def fail_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected queue projection failure")

    def release_then_replace(registry, lease) -> None:
        real_release(registry, lease)
        successor.append(
            registry.acquire(
                "queue-worker",
                scope="worker:test",
                actor_id="successor-actor",
                token="successor-token",
            )
        )

    monkeypatch.setattr(queue, "_insert_queue_projection", fail_projection)
    monkeypatch.setattr(
        operational_ownership.OwnershipRegistry, "release", release_then_replace
    )

    with pytest.raises(RuntimeError, match="injected queue projection failure"):
        with queue.queue_owner(role="queue-worker", scope="worker:test"):
            pytest.fail("projection failure must happen before the body")

    with sqlite3.connect(
        tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    ) as database:
        assert database.execute(
            "SELECT owner_token, fencing_epoch FROM maintenance_owners "
            "WHERE role='queue-worker' AND scope='worker:test'"
        ).fetchone() == (successor[0].token, successor[0].epoch)


def test_queue_release_removes_projection_before_canonical_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _v3_queue(tmp_path)
    observed: list[tuple[int, int]] = []
    real_remove = queue._remove_queue_projection

    def observe_release(lease) -> None:
        with sqlite3.connect(queue.db_path) as queue_database, sqlite3.connect(
            tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
        ) as coordinator_database:
            observed.append(
                (
                    queue_database.execute(
                        "SELECT COUNT(*) FROM queue_ownership WHERE owner_token=?",
                        (lease.token,),
                    ).fetchone()[0],
                    coordinator_database.execute(
                        "SELECT COUNT(*) FROM maintenance_owners WHERE owner_token=?",
                        (lease.token,),
                    ).fetchone()[0],
                )
            )
        real_remove(lease)
        with sqlite3.connect(queue.db_path) as queue_database, sqlite3.connect(
            tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
        ) as coordinator_database:
            observed.append(
                (
                    queue_database.execute(
                        "SELECT COUNT(*) FROM queue_ownership WHERE owner_token=?",
                        (lease.token,),
                    ).fetchone()[0],
                    coordinator_database.execute(
                        "SELECT COUNT(*) FROM maintenance_owners WHERE owner_token=?",
                        (lease.token,),
                    ).fetchone()[0],
                )
            )

    monkeypatch.setattr(queue, "_remove_queue_projection", observe_release)
    with queue.queue_owner(role="queue-worker", scope="worker:release") as lease:
        assert lease.role == "queue-worker"
        queue.heartbeat_queue_owner(lease)
        with sqlite3.connect(queue.db_path) as database:
            projection = database.execute(
                "SELECT heartbeat_at, expires_at FROM queue_ownership "
                "WHERE owner_token=?",
                (lease.token,),
            ).fetchone()
        with sqlite3.connect(
            tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
        ) as database:
            canonical = database.execute(
                "SELECT heartbeat_at, expires_at FROM maintenance_owners "
                "WHERE owner_token=?",
                (lease.token,),
            ).fetchone()
        assert tuple(
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in projection
        ) == tuple(
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in canonical
        )

    assert observed == [(1, 1), (0, 1)]
    with sqlite3.connect(
        tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    ) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM maintenance_owners WHERE owner_token=?", (lease.token,)
        ).fetchone() == (0,)


def test_nested_queue_owner_projects_parent_without_releasing_it(tmp_path: Path) -> None:
    queue = _v3_queue(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    parent = registry.acquire("nightly", scope="global", marker=_maintenance_marker(tmp_path))

    with queue.queue_owner(
        role="queue-worker", scope="worker:nested", parent=parent
    ) as projected:
        assert projected == parent
        with sqlite3.connect(queue.db_path) as database:
            assert database.execute(
                "SELECT canonical_role, canonical_scope, owner_token, fencing_epoch "
                "FROM queue_ownership"
            ).fetchone() == ("nightly", "global", parent.token, parent.epoch)

    with sqlite3.connect(
        tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    ) as database:
        assert database.execute(
            "SELECT owner_token FROM maintenance_owners WHERE role='nightly'"
        ).fetchone() == (parent.token,)
    registry.release(parent)


def _maintenance_marker(state_root: Path) -> operational_ownership.MarkerIdentity:
    path = state_root / "run/maintenance.lock"
    path.write_bytes(str(os.getpid()).encode("ascii"))
    from reliable_memory import capture_runtime_file_identity, sha256_bytes

    return operational_ownership.MarkerIdentity(
        relative_path="run/maintenance.lock",
        sha256=sha256_bytes(path.read_bytes()),
        file_identity=capture_runtime_file_identity(path, state_root=state_root),
        pid=os.getpid(),
    )


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
    assert counts == {"ok": 1, "failed": 0, "dead": 0, "skipped": 0}
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
    assert counts == {"ok": 0, "failed": 1, "dead": 0, "skipped": 0}
    task = primary.get(task_id)
    assert replacement[0] is not None
    assert task.state == "leased"
    assert task.lease_owner == "replacement"
    assert task.result_reference is None
    assert not any(
        thread.name == f"memory-queue-heartbeat-{task_id}" and thread.is_alive()
        for thread in threading.enumerate()
    )
