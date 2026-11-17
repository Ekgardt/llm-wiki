"""Helpers shared by test_capture_intent_adoption.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import capture_adoption
import markdown_transaction
import queue_core
from reliable_memory import canonical_json_bytes, publish_runtime_file, sha256_bytes


def _queue(tmp_path: Path):
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    markdown_transaction.initialize_coordinator_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3", source_v2=None
    )
    queue_core.initialize_queue_v3_candidate(
        tmp_path / "run" / "queue-v3.candidate.sqlite3", source_v2=None
    )
    return queue_core.MemoryQueue.from_v3_candidate(
        tmp_path / "run" / "queue-v3.candidate.sqlite3", state_root=tmp_path
    )


def _coordinator(tmp_path: Path):
    return markdown_transaction.MarkdownCoordinator.from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )


def _session_intent(seed: bytes) -> tuple[str, bytes]:
    from tests.adopted_capture_vault import session_intent_payload

    return session_intent_payload(seed)


def _publish_ready_intent(tmp_path: Path, queue, coordinator, seed: bytes) -> dict:
    """Every publication step for real, up to but not including the enqueue."""
    intent_id, payload = _session_intent(seed)
    digest = sha256_bytes(payload)
    shard = intent_id[:2]
    pending = f"run/capture-intents/pending/{shard}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{shard}/{intent_id}.json"
    for relative in (pending, ready):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)

    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    descriptor = {
        "intent_id": intent_id,
        "pending_path": pending,
        "ready_path": ready,
        "intent_sha256": digest,
        "byte_size": len(payload),
    }
    publish_runtime_file(
        tmp_path / pending, payload, state_root=tmp_path, create_only=True, mode=0o600
    )
    queue.index_capture_intent_pending(**descriptor)
    publish_runtime_file(
        tmp_path / ready, payload, state_root=tmp_path, create_only=True, mode=0o600
    )
    queue.mark_capture_intent_ready(**descriptor)
    (tmp_path / pending).unlink()
    # The loss: the fence and the owner are gone before the task is enqueued.
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return {
        "intent_id": intent_id,
        "ready": ready,
        "sha256": digest,
        "fence_token": fence.token,
        "fence_epoch": fence.epoch,
    }


def _links(queue) -> list[sqlite3.Row]:
    database = sqlite3.connect(str(queue.db_path))
    database.row_factory = sqlite3.Row
    try:
        return database.execute("SELECT * FROM capture_task_links").fetchall()
    finally:
        database.close()


def _adopted_ids(result: dict) -> list[str]:
    return [entry["intent_id"] for entry in result["adopted"]]


def _skipped_ids(result: dict) -> list[str]:
    return [entry["intent_id"] for entry in result["skipped"]]


def _linked_ids(queue) -> list[str]:
    return [row["intent_id"] for row in _links(queue)]


def _adopt(queue, coordinator, tmp_path: Path, **kwargs) -> dict:
    return capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path, **kwargs
    )


def _damage(tmp_path: Path, orphan: dict) -> None:
    target = tmp_path / str(orphan["ready"])
    target.chmod(0o600)
    target.write_bytes(canonical_json_bytes({"seed": "tampered"}))
