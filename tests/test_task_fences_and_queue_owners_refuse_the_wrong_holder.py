from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import markdown_transaction
import pytest
import queue_core
from llm_wiki.queue_contracts import QueueOperationError
from operational_ownership import OwnerLease, OwnershipRegistry
from queue_core import QueueV3CandidateReader

CORRUPT_PAYLOAD = b'{"payload":"corrupt"}'


@pytest.fixture
def queue(tmp_path: Path) -> QueueV3CandidateReader:
    coordinator = tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run/queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return queue_core.MemoryQueue.from_v3_candidate(candidate, state_root=tmp_path)


@pytest.fixture
def repair(tmp_path: Path) -> Iterator[OwnerLease]:
    registry = OwnershipRegistry(tmp_path)
    owner = registry.acquire("repair", scope="repair:fences", actor_id="repairer")
    yield owner
    registry.release(owner)


def _untyped(value: object) -> Any:
    return cast(Any, value)


def test_a_queue_owner_of_an_unknown_role_is_refused(queue: QueueV3CandidateReader) -> None:
    with (
        pytest.raises(ValueError, match="queue-worker or queue-operator"),
        queue.queue_owner(role=_untyped("queue-admin"), scope="worker:x"),
    ):
        pass


def test_a_queue_owner_parent_must_be_an_owner_lease(queue: QueueV3CandidateReader) -> None:
    with (
        pytest.raises(TypeError, match="parent must be an OwnerLease"),
        queue.queue_owner(role="queue-worker", scope="worker:x", parent=_untyped("lease")),
    ):
        pass


def test_a_repair_parent_cannot_project_a_worker(
    queue: QueueV3CandidateReader, repair: OwnerLease
) -> None:
    with (
        pytest.raises(ValueError, match="parent role cannot project"),
        queue.queue_owner(role="queue-worker", scope="worker:x", parent=repair),
    ):
        pass


def test_a_queue_projection_needs_an_owner_lease(queue: QueueV3CandidateReader) -> None:
    with pytest.raises(TypeError, match="lease must be an OwnerLease"):
        queue._insert_queue_projection(_untyped("lease"), role="queue-worker", scope="worker:x")
    with pytest.raises(TypeError, match="lease must be an OwnerLease"):
        queue._remove_queue_projection(_untyped("lease"))


def test_removing_a_projection_twice_reports_the_lost_fence(
    queue: QueueV3CandidateReader,
) -> None:
    with queue.queue_owner(role="queue-worker", scope="worker:x") as owner:
        queue._remove_queue_projection(owner)
        with pytest.raises(QueueOperationError, match="queue_owner_fence_lost"):
            queue._remove_queue_projection(owner)
        queue._insert_queue_projection(owner, role="queue-worker", scope="worker:x")


def test_a_task_fence_needs_an_owner_lease(queue: QueueV3CandidateReader) -> None:
    task_id = queue.enqueue("query", 1, {"query": "requested"})

    with pytest.raises(TypeError, match="owner must be an OwnerLease"):
        queue.acquire_task_fence(task_id, mode="worker", owner=_untyped("owner"))


def test_a_worker_cannot_take_an_operator_fence(queue: QueueV3CandidateReader) -> None:
    task_id = queue.enqueue("query", 1, {"query": "requested"})

    with (
        queue.queue_owner(role="queue-worker", scope="worker:x") as owner,
        pytest.raises(ValueError, match="cannot acquire the requested task fence"),
    ):
        queue.acquire_task_fence(task_id, mode="queue-operator", owner=owner)


def test_a_task_fence_needs_a_task_id(queue: QueueV3CandidateReader) -> None:
    with (
        queue.queue_owner(role="queue-worker", scope="worker:x") as owner,
        pytest.raises(ValueError, match="task_id must be non-empty"),
    ):
        queue.acquire_task_fence("", mode="worker", owner=owner)


def test_a_fence_over_a_missing_task_names_it(queue: QueueV3CandidateReader) -> None:
    with (
        queue.queue_owner(role="queue-worker", scope="worker:x") as owner,
        pytest.raises(KeyError, match="no-such-task"),
    ):
        queue.acquire_task_fence("no-such-task", mode="worker", owner=owner)


def test_a_fenced_task_cannot_be_fenced_again_and_releases_once(
    queue: QueueV3CandidateReader,
) -> None:
    task_id = queue.enqueue("query", 1, {"query": "requested"})

    with queue.queue_owner(role="queue-worker", scope="worker:x") as owner:
        fence = queue.acquire_task_fence(task_id, mode="worker", owner=owner)
        with pytest.raises(QueueOperationError, match="task_fenced"):
            queue.acquire_task_fence(task_id, mode="worker", owner=owner)
        queue.release_task_fence(fence)
        with pytest.raises(QueueOperationError, match="task_fence_lost"):
            queue.release_task_fence(fence)


def test_releasing_something_that_is_not_a_fence_is_refused(
    queue: QueueV3CandidateReader,
) -> None:
    with pytest.raises(TypeError, match="fence must be a TaskFence"):
        queue.release_task_fence(_untyped("fence"))


def test_corrupt_quarantine_needs_a_repair_owner(queue: QueueV3CandidateReader) -> None:
    with (
        queue.queue_owner(role="queue-worker", scope="worker:x") as owner,
        pytest.raises(ValueError, match="requires a repair owner"),
    ):
        queue.quarantine_corrupt("task", reason="Retain evidence.", owner=owner)


def test_a_healthy_task_cannot_be_quarantined_as_corrupt(
    queue: QueueV3CandidateReader, repair: OwnerLease
) -> None:
    task_id = queue.enqueue("query", 1, {"query": "requested"})

    with pytest.raises(QueueOperationError, match="corrupt_quarantine_state_invalid"):
        queue.quarantine_corrupt(task_id, reason="Retain evidence.", owner=repair)

    assert queue.get(task_id).state == "ready"


def _insert_corrupt_task(queue: QueueV3CandidateReader, task_id: str) -> None:
    now = "2026-08-12T12:00:00+00:00"
    with closing(sqlite3.connect(queue.db_path)) as database, database:
        database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at,error_code
               ) VALUES (?,'query',1,?,?,'dead',0,?,?,?,'payload_hash_mismatch')""",
            (task_id, CORRUPT_PAYLOAD, "0" * 64, now, now, now),
        )


def test_a_second_quarantine_with_another_reason_conflicts_with_the_first(
    queue: QueueV3CandidateReader, repair: OwnerLease
) -> None:
    _insert_corrupt_task(queue, "corrupt")
    first = queue.quarantine_corrupt("corrupt", reason="First reason.", owner=repair)

    with pytest.raises(QueueOperationError, match="corrupt_export_conflict"):
        queue.quarantine_corrupt("corrupt", reason="Second reason.", owner=repair)

    again = queue.quarantine_corrupt("corrupt", reason="First reason.", owner=repair)
    assert again.operation_id == first.operation_id
