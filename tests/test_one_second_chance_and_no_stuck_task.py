"""A lineage gets one redrive, and a task out of attempts is dead, never stuck ready.

The bound read the dead row's own count, which a copy resets; an expired eighth lease
left a ready task nothing could take or redrive; and the adopted claim never recovered
an expired lease. Research: `docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, QueueFailure, QueueOperationError  # noqa: E402

PAST = "2000-01-01T00:00:00+00:00"


def _v3_queue(tmp_path: Path):
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)


def _killed(queue, payload: dict) -> str:
    task_id = queue.enqueue("query", 1, payload)
    lease = queue.claim("worker")
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    return task_id


def _die(queue, task_id: str) -> None:
    lease = queue.claim("worker")
    assert lease is not None and lease.id == task_id
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))


def test_the_copy_of_a_redriven_task_cannot_be_redriven_again(tmp_path):
    queue = _v3_queue(tmp_path)
    child = queue.redrive(_killed(queue, {"case": "lineage"}))
    _die(queue, child)

    with pytest.raises(QueueOperationError, match="redrive_generations_exhausted"):
        queue.redrive(child)


def test_the_legacy_queue_bounds_the_lineage_too(tmp_path):
    queue = MemoryQueue(tmp_path, rng=random.Random(3))
    child = queue.redrive(_killed(queue, {"case": "legacy"}))
    _die(queue, child)

    with pytest.raises(QueueOperationError, match="redrive_generations_exhausted"):
        queue.redrive(child)


def _expire(queue, task_id: str, attempts: int) -> None:
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET attempts=?, lease_expires_at=? WHERE id=?", (attempts, PAST, task_id)
        )


def test_a_lease_that_expired_on_the_last_attempt_leaves_a_dead_task(tmp_path):
    queue = _v3_queue(tmp_path)
    task_id = queue.enqueue("query", 1, {"case": "last"})
    queue.claim("worker")
    _expire(queue, task_id, memory_queue.DEFAULTS.queue_max_attempts)

    queue.recover_expired_leases()

    assert (queue.get(task_id).state, queue.get(task_id).error_code) == ("dead", "attempts_exhausted")


def test_a_claim_recovers_an_expired_lease_before_it_chooses(tmp_path):
    queue = _v3_queue(tmp_path)
    task_id = queue.enqueue("query", 1, {"case": "abandoned"})
    queue.claim("worker")
    _expire(queue, task_id, 1)

    lease = queue.claim("second-worker")

    assert (lease.id, lease.attempt) == (task_id, 2)
