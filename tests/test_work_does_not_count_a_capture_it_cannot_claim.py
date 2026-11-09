"""`work` does not count a capture task it cannot claim.

A capture task belongs to the capture worker (`claim_capture`); `work`'s claim
excludes it on purpose. The eligible count did not, so a capture waiting on its
retry made `work` report `remaining_eligible: 1` with nothing processed, and the
nightly's work step failed on every pass until a capture worker ran (2026-09-28).
"""

from __future__ import annotations

import sys
from pathlib import Path

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
