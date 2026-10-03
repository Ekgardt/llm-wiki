"""`work` does not count a capture task it cannot claim.

A capture task belongs to the capture worker (`claim_capture`); `work`'s claim
excludes it on purpose. The eligible count did not, so a capture waiting on its
retry made `work` report `remaining_eligible: 1` with nothing processed, and the
nightly's work step failed on every pass until a capture worker ran (2026-09-28).
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)


def _ready_capture(tmp_path: Path, queue, intent_id: str) -> str:
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3", state_root=tmp_path
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id, intent_path=intent_path, intent_sha256="2" * 64, byte_size=128
    )
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": intent_id},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256="2" * 64,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return binding.task_id


def test_a_ready_capture_is_not_counted_as_work(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _ready_capture(tmp_path, queue, "a" * 64)

    assert queue.claim("worker", lease_seconds=60) is None
    assert queue.count_eligible() == 0


def test_ordinary_work_is_still_counted_beside_a_capture(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _ready_capture(tmp_path, queue, "b" * 64)
    queue.enqueue("query", 1, {"prompt": "counted"})

    assert queue.count_eligible() == 1


@pytest.mark.parametrize("backend", ["legacy", "adopted"])
def test_a_task_of_the_fenced_source_is_neither_counted_nor_claimed(
    tmp_path: Path, backend: str
) -> None:
    """The class, not only the capture: whatever the claim skips, the count skips.

    A task of a fenced day waits for its fence; counting it failed the nightly's
    work step the same way (2026-09-28,
    docs/research/2026-09-28-a-check-names-its-cause.md). `acquire_source_fence`
    refuses a source a live task names, so the fence row is written the way that
    method writes it, without its check: what is tested is the claim's own guard.
    """
    queue = _backend(tmp_path, backend)
    queue.enqueue("compile", 1, {"daily_id": "2026-01-01"})
    _FENCE_WRITERS[backend](queue, "2026-01-01", "c" * 64)

    assert (queue.count_eligible(), queue.claim("worker", lease_seconds=60)) == (0, None)


@pytest.mark.parametrize("backend", ["legacy", "adopted"])
def test_a_task_that_only_mentions_the_fenced_day_is_counted_and_claimed(
    tmp_path: Path, backend: str
) -> None:
    """A fence holds its source, not every payload that carries its date.

    The claim and the count matched a fence with `instr` over the payload text, so
    a task written on the fenced day was skipped and not counted, while the
    enqueue and the fence check read identity fields only. Both now call that one
    rule (2026-09-28).
    """
    queue = _backend(tmp_path, backend)
    task_id = queue.enqueue(
        "query", 1, {"prompt": "written", "written_at": "2026-01-01T09:00:00Z"}
    )
    fence = queue.acquire_source_fence("2026-01-01", "c" * 64)

    counted = queue.count_eligible()
    claimed = queue.claim("worker", lease_seconds=60)

    queue.release_source_fence(fence.token)
    assert (counted, getattr(claimed, "id", None)) == (1, task_id)


def _backend(tmp_path: Path, backend: str):
    if backend == "adopted":
        return _queue(tmp_path)
    return memory_queue.MemoryQueue(tmp_path)


def _legacy_fence(queue, daily_id: str, digest: str) -> None:
    now = memory_queue._timestamp(memory_queue._utc_now())
    later = memory_queue._timestamp(memory_queue._utc_now() + timedelta(hours=1))
    with queue._connect() as connection:
        memory_queue._insert_source_fence(
            connection, (daily_id, digest, "f" * 64, os.getpid(), now, now, later)
        )


def _adopted_fence(queue, daily_id: str, digest: str) -> None:
    process = operational_ownership.current_process_identity()
    now = memory_queue._timestamp(memory_queue._utc_now())
    later = memory_queue._timestamp(memory_queue._utc_now() + timedelta(hours=1))
    values = (
        memory_queue._daily_logical_path(daily_id), digest, "f" * 64,
        process.pid, process.start_identity, now, now, later,
    )
    with closing(queue._connect()) as database, database:
        memory_queue._insert_adopted_source_fence(database, values)


_FENCE_WRITERS = {"legacy": _legacy_fence, "adopted": _adopted_fence}
