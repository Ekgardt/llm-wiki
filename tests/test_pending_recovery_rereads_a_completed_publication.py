"""A listed pending file can become verified ready evidence before recovery reads it."""
from __future__ import annotations

import breadcrumb_storage as storage
import pytest
from reliable_memory import canonical_json_bytes

from tests.test_breadcrumb_adoption import _orphan


def _complete_before_read(monkeypatch, queue, coordinator, identity, damage=None):
    original = storage._recover_pending_file
    manifest = storage._stored_manifest(queue.state_root, identity)

    def recover(current_queue, current_coordinator, path):
        storage.register_manifest(queue, coordinator, manifest)
        if damage is not None:
            damage(manifest)
        return original(current_queue, current_coordinator, path)

    monkeypatch.setattr(storage, "_recover_pending_file", recover)


@pytest.mark.parametrize("stage", ["index_capture_intent_pending", "mark_capture_intent_ready", "enqueue_capture_task_replay_safe"])
def test_a_publisher_finishing_after_discovery_is_verified_without_a_false_loss(tmp_path, monkeypatch, stage):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, stage)
    _complete_before_read(monkeypatch, queue, coordinator, identity)

    result = storage.recover_pending(queue, coordinator)

    assert result == {"recovered": [identity], "skipped": [], "legacy": 0}
    bundle = storage.load_bundle(tmp_path, storage._stored_manifest(tmp_path, identity))
    assert bundle.content == canonical_json_bytes({"prompt": "complete prompt"})
    lease = queue.claim_capture("verified", handler_versions=(2,))
    assert lease is not None and lease.payload["intent_id"] == identity
    assert queue.claim_capture("verified", handler_versions=(2,)) is None


def test_missing_pending_without_ready_evidence_is_still_reported(tmp_path, monkeypatch):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    path = storage.anchor_path(tmp_path, identity).with_suffix(".json")
    original = storage._recover_pending_file

    def disappear(current_queue, current_coordinator, listed):
        path.unlink()
        return original(current_queue, current_coordinator, listed)

    monkeypatch.setattr(storage, "_recover_pending_file", disappear)
    result = storage.recover_pending(queue, coordinator)
    assert result["recovered"] == []
    assert [item["intent_id"] for item in result["skipped"]] == [identity]
    assert "FileNotFoundError" in result["skipped"][0]["reason"]
    assert queue.claim_capture("missing", handler_versions=(2,)) is None
    assert storage.anchor_path(tmp_path, identity).is_file()


@pytest.mark.parametrize("damage", ["manifest", "part"])
def test_a_completed_path_is_not_proof_when_its_evidence_is_damaged(tmp_path, monkeypatch, damage):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")

    def damage_evidence(manifest):
        paths = {
            "manifest": tmp_path / "run/capture-intents/ready" / identity[:2] / f"{identity}.json",
            "part": storage.part_path(tmp_path, identity, storage.protocol.read_manifest(manifest)["last_part_sha256"]),
        }
        paths[damage].write_bytes(b"damaged evidence")

    _complete_before_read(monkeypatch, queue, coordinator, identity, damage_evidence)
    result = storage.recover_pending(queue, coordinator)
    assert result["recovered"] == []
    assert [item["intent_id"] for item in result["skipped"]] == [identity]
    assert storage.anchor_path(tmp_path, identity).is_file()
