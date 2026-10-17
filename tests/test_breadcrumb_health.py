"""Runtime inspection must verify the entire retained breadcrumb source."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import breadcrumb_storage as storage
import doctor
import installed_memory_repair
import integration_adapter
import markdown_transaction
import memory_queue
import pytest

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_breadcrumb_storage import _bundle, _publish, _registration_failure
from tests.test_reliability_v3_adoption import _inventory, _vault, build_adopted_reliability_v3


def _runtime(tmp_path):
    vault, state = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state)
    queue = memory_queue.active_memory_queue(vault, state)
    coordinator = markdown_transaction.active_markdown_coordinator(vault, state)
    return state, queue, coordinator


@pytest.fixture
def runtime(tmp_path):
    return _runtime(tmp_path)


@pytest.fixture
def retained(runtime):
    state, queue, coordinator = runtime
    publication = _publish(queue, coordinator)
    return state, publication.intent_id


def _inspect(state):
    return installed_memory_repair.validate_queue_v3_runtime(
        state_root=state, now=datetime.now(timezone.utc),
        deadline=time.monotonic() + SHORT_TIMEOUT, excluded_owner=None,
    )


def test_intact_breadcrumb_is_retained_and_readable(retained):
    state, _identity = retained
    result = _inspect(state)
    assert "capture_intent_retained" in result
    assert "queue_state_unreadable" not in result


@pytest.mark.parametrize("damage", ["missing_part", "changed_part", "missing_anchor"])
def test_inspection_refuses_damaged_linked_evidence(retained, damage):
    state, identity = retained
    manifest = storage.protocol.read_manifest(_bundle(state, identity).manifest)
    paths = {
        "missing_part": storage.part_path(state, identity, manifest["last_part_sha256"]),
        "changed_part": storage.part_path(state, identity, manifest["last_part_sha256"]),
        "missing_anchor": storage.anchor_path(state, identity),
    }
    path = paths[damage]
    if damage == "changed_part":
        path.write_bytes(b"{}")
    else:
        path.unlink()
    assert "queue_state_unreadable" in _inspect(state)
    assert storage._stored_manifest(state, identity)


def test_doctor_names_a_missing_part_as_an_error(retained):
    state, identity = retained
    manifest = storage.protocol.read_manifest(_bundle(state, identity).manifest)
    storage.part_path(state, identity, manifest["last_part_sha256"]).unlink()
    result = doctor._queue_check(state, datetime.now(timezone.utc), time.monotonic() + 30)
    assert result["status"] == "error"
    assert "capture" in result["message"].lower()
    assert "FileNotFoundError" in result["details"]["capture_intent_error"]


def test_inspection_deadline_applies_between_linked_records(retained, monkeypatch):
    state, _identity = retained
    clock = [0.0]
    original = storage._read

    def read_then_expire(root, path):
        data = original(root, path)
        clock[0] = 31.0
        return data

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(storage, "_read", read_then_expire)
    with pytest.raises(TimeoutError, match="deadline"):
        _inspect(state)


def test_doctor_reports_a_complete_unindexed_publication(runtime, monkeypatch):
    state, queue, coordinator = runtime
    monkeypatch.setattr(queue, "index_capture_intent_pending", _registration_failure)
    publication = _publish(queue, coordinator)
    assert publication.registered is False
    before = _inventory(state)
    result = doctor._queue_check(state, datetime.now(timezone.utc), time.monotonic() + 30)
    assert _inventory(state) == before
    assert result["status"] == "degraded"
    assert result["details"]["capture_pending_complete"] == 1
    assert "pending" in result["message"].lower()
    assert queue.claim_capture("test", handler_versions=(2,)) is None


def _manifest_write_failed(*args, **kwargs):
    raise OSError("manifest publication interrupted")


def test_doctor_reports_partial_publication_without_accepting_it(runtime, monkeypatch):
    state, queue, coordinator = runtime
    monkeypatch.setattr(storage, "_store_manifest", _manifest_write_failed)
    with pytest.raises(OSError, match="manifest publication interrupted"):
        _publish(queue, coordinator)
    result = doctor._queue_check(state, datetime.now(timezone.utc), time.monotonic() + 30)
    assert result["status"] == "degraded"
    assert result["details"]["capture_incomplete"] == 1
    assert "incomplete" in result["message"].lower()
    assert queue.claim_capture("test", handler_versions=(2,)) is None


def test_doctor_reports_an_unreadable_unindexed_shard(runtime, monkeypatch):
    state, queue, coordinator = runtime
    monkeypatch.setattr(queue, "index_capture_intent_pending", _registration_failure)
    publication = _publish(queue, coordinator)
    shard = storage.anchor_path(state, publication.intent_id).parent
    relocated = state / "relocated"
    shard.rename(relocated)
    shard.symlink_to(relocated, target_is_directory=True)
    result = doctor._queue_check(state, datetime.now(timezone.utc), time.monotonic() + 30)
    assert result["status"] == "error"
    assert "capture intent directory is unsafe" in result["details"]["capture_intent_error"]


def test_inspection_deadline_applies_to_empty_directories(runtime, monkeypatch):
    state, _queue, _coordinator = runtime
    (state / "run/capture-intents/pending/aa").mkdir(parents=True)
    clock = [0.0]
    original = integration_adapter._validate_capture_directory

    def validate_then_expire(path, root):
        original(path, root)
        clock[0] = 100.0

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(integration_adapter, "_validate_capture_directory", validate_then_expire)
    with pytest.raises(TimeoutError, match="deadline"):
        storage.inspect_pending_sources(state, set(), deadline=10.0)
