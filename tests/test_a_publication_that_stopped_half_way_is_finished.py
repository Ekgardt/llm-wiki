"""A publisher killed before it marked an intent ready must not lose the session.

See `docs/research/2026-09-17-a-publication-that-stopped-half-way-is-finished.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from reliable_memory import canonical_json_bytes, publish_runtime_file, sha256_bytes

from tests.test_capture_intent_adoption import (
    _coordinator,
    _linked_ids,
    _queue,
    _skipped_ids,
)

LATER = datetime.now(timezone.utc) + timedelta(hours=1)


def _publish_pending_intent(tmp_path: Path, queue, coordinator, seed: bytes) -> dict:
    """The publication, stopped where a host timeout stops it: the pending row and file."""
    payload = canonical_json_bytes({"seed": seed.decode()})
    intent_id = sha256_bytes(seed)
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


def test_the_half_published_intent_this_recovers_from_is_real(tmp_path, monkeypatch):
    """Guard the premise: the ready-state sweeper cannot see a pending row."""
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"pending-premise")

    orphans = queue.ready_capture_intents_without_task(32)

    assert (_pending_ids(queue), orphans, (tmp_path / half["pending"]).exists()) == (
        [half["intent_id"]],
        [],
        True,
    )


def test_a_pending_intent_is_published_and_given_a_task(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"finish-me")

    result = _complete(queue, coordinator, tmp_path, now=LATER)

    assert (
        [entry["intent_id"] for entry in result["completed"]],
        _linked_ids(queue),
        _pending_ids(queue),
        ((tmp_path / half["ready"]).exists(), (tmp_path / half["pending"]).exists()),
    ) == ([half["intent_id"]], [half["intent_id"]], [], (True, False))


def test_a_pending_intent_younger_than_its_fence_is_left_to_its_publisher(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"still-running")

    result = _complete(queue, coordinator, tmp_path)

    assert (result["examined"], _pending_ids(queue)) == (0, [half["intent_id"]])


def test_a_pending_record_whose_bytes_moved_is_a_named_skip(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"tampered")
    target = tmp_path / half["pending"]
    target.chmod(0o600)
    target.write_bytes(canonical_json_bytes({"seed": "tampered-with"}))

    result = _complete(queue, coordinator, tmp_path, now=LATER)

    assert (_skipped_ids(result), result["completed"]) == ([half["intent_id"]], [])
