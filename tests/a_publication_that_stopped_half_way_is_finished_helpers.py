"""Helpers shared by test_a_publication_that_stopped_half_way_is_finished.py and the test files importing them."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from reliable_memory import publish_runtime_file, sha256_bytes

from tests.capture_intent_adoption_helpers import _session_intent

LATER = datetime.now(UTC) + timedelta(hours=1)


def _publish_pending_intent(tmp_path: Path, queue, coordinator, seed: bytes) -> dict:
    """The publication, stopped where a host timeout stops it: the pending row and file."""
    intent_id, payload = _session_intent(seed)
    shard = intent_id[:2]
    pending = f"run/capture-intents/pending/{shard}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{shard}/{intent_id}.json"
    for relative in (pending, ready):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)

    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    publish_runtime_file(
        tmp_path / pending, payload, state_root=tmp_path, create_only=True, mode=0o600
    )
    queue.index_capture_intent_pending(
        intent_id=intent_id,
        pending_path=pending,
        ready_path=ready,
        intent_sha256=sha256_bytes(payload),
        byte_size=len(payload),
    )
    # The loss: the publisher dies here, before `mark_capture_intent_ready`.
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return {"intent_id": intent_id, "pending": pending, "ready": ready}


def _complete(queue, coordinator, tmp_path: Path, **kwargs) -> dict:
    import capture_adoption

    return capture_adoption.complete_pending_capture_intents(
        queue, coordinator, state_root=tmp_path, **kwargs
    )


def _pending_ids(queue) -> list[str]:
    return [str(row["intent_id"]) for row in queue.pending_capture_intents(32)]
