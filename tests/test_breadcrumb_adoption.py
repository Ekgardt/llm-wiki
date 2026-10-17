"""Recovery must select the format's worker and verify every durable part."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import breadcrumb_storage as storage
import capture_adoption
import flush_memory
import integration_adapter
import pytest

from tests.test_breadcrumb_storage import _bundle, _publish, _registration_failure
from tests.test_capture_intent_adoption import _session_intent
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _orphan(root, monkeypatch, stage):
    queue, coordinator = _queue(root), _coordinator(root)
    with monkeypatch.context() as patch:
        patch.setattr(queue, stage, _registration_failure)
        publication = _publish(queue, coordinator)
    assert publication.registered is False
    return queue, coordinator, publication.intent_id


@pytest.mark.parametrize("stage", ["enqueue_capture_task_replay_safe", "mark_capture_intent_ready"])
def test_database_recovery_preserves_the_breadcrumb_handler(tmp_path, monkeypatch, stage):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, stage)
    capture_adoption.complete_pending_capture_intents(
        queue, coordinator, state_root=tmp_path,
        now=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    result = capture_adoption.adopt_orphaned_capture_intents(queue, coordinator, state_root=tmp_path)
    assert result["skipped"] == []
    assert queue.claim_capture("legacy", handler_versions=(1,)) is None
    lease = queue.claim_capture("breadcrumb", handler_versions=(2,))
    assert lease is not None
    assert lease.payload["intent_id"] == identity


@pytest.mark.parametrize("stage", ["enqueue_capture_task_replay_safe", "mark_capture_intent_ready"])
def test_database_recovery_refuses_missing_parts(tmp_path, monkeypatch, stage):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, stage)
    bundle = _bundle(tmp_path, identity)
    digest = storage.protocol.read_manifest(bundle.manifest)["last_part_sha256"]
    storage.part_path(tmp_path, identity, digest).unlink()
    pending = capture_adoption.complete_pending_capture_intents(
        queue, coordinator, state_root=tmp_path,
        now=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    ready = capture_adoption.adopt_orphaned_capture_intents(queue, coordinator, state_root=tmp_path)
    assert pending["skipped"] or ready["skipped"]
    assert queue.claim_capture("any", handler_versions=(1, 2)) is None
    assert storage._stored_manifest(tmp_path, identity) == bundle.manifest


def test_worker_discovers_a_complete_record_without_a_database_row(tmp_path, monkeypatch):
    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    flush_memory._adopt_orphaned_intents(queue, coordinator)
    lease = queue.claim_capture("breadcrumb", handler_versions=(2,))
    assert lease is not None
    assert lease.payload["intent_id"] == identity


def test_nightly_discovers_a_complete_record_without_a_database_row(tmp_path, monkeypatch, capsys):
    from tests.test_the_nightly_finishes_what_a_publisher_left_half_way import _active_vault

    queue, coordinator, identity = _orphan(tmp_path, monkeypatch, "index_capture_intent_pending")
    _active_vault(tmp_path, monkeypatch, queue, coordinator)
    result = capture_adoption.adopt_in_active_vault()
    lease = queue.claim_capture("breadcrumb", handler_versions=(2,))
    assert lease is not None
    assert lease.payload["intent_id"] == identity
    capture_adoption._report(result)
    assert "recovered 1 disk manifests" in capsys.readouterr().out


def test_physical_recovery_preserves_unindexed_legacy_sessions(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    identity, payload = _session_intent(b"legacy-before-index")
    integration_adapter._ensure_capture_intent_directories(tmp_path, identity)
    storage._store_manifest(tmp_path, identity, payload)
    result = storage.recover_pending(queue, coordinator)
    assert result["recovered"] == [identity]
    assert result["legacy"] == 1
    assert result["skipped"] == []
    lease = queue.claim_capture("legacy", handler_versions=(1,))
    assert lease is not None
    assert lease.payload["intent_id"] == identity
    assert storage.recover_pending(queue, coordinator)["recovered"] == []
