"""Replay stale pending descriptors only with exact verified publication bytes."""
import pytest

from tests.test_a_publication_that_stopped_half_way_is_finished import (
    LATER,
    _complete,
    _publish_pending_intent,
)
from tests.test_capture_intent_adoption import _coordinator, _links, _queue


def _completed_snapshot(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"stale-pending")
    record = queue.pending_capture_intents(1)[0]
    first = _complete(queue, coordinator, tmp_path, now=LATER)
    assert first["completed"] == [{"intent_id": half["intent_id"]}]
    assert not (tmp_path / half["pending"]).exists()
    return queue, coordinator, half, record


def test_stale_pending_snapshot_replays_the_same_verified_task(tmp_path, monkeypatch):
    import capture_adoption

    queue, coordinator, half, record = _completed_snapshot(tmp_path, monkeypatch)
    original = dict(_links(queue)[0])
    completed = capture_adoption._complete_one_pending(queue, coordinator, tmp_path, record)
    assert completed == half["intent_id"]
    assert [dict(row) for row in _links(queue)] == [original]
    assert not (tmp_path / half["pending"]).exists()


def test_stale_snapshot_does_not_accept_damaged_ready_bytes(tmp_path, monkeypatch):
    import capture_adoption

    queue, coordinator, half, record = _completed_snapshot(tmp_path, monkeypatch)
    (tmp_path / half["ready"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="intent_digest_changed"):
        capture_adoption._complete_one_pending(queue, coordinator, tmp_path, record)
    assert len(_links(queue)) == 1


def test_stale_snapshot_does_not_accept_missing_both_copies(tmp_path, monkeypatch):
    import capture_adoption

    queue, coordinator, half, record = _completed_snapshot(tmp_path, monkeypatch)
    (tmp_path / half["ready"]).unlink()
    with pytest.raises(FileNotFoundError):
        capture_adoption._complete_one_pending(queue, coordinator, tmp_path, record)
    assert len(_links(queue)) == 1


def test_pending_permission_error_does_not_try_a_different_copy(tmp_path, monkeypatch):
    import capture_adoption
    import reliable_memory

    queue, coordinator, half, record = _completed_snapshot(tmp_path, monkeypatch)
    calls = []

    def denied(path, *_args, **_kwargs):
        calls.append(path)
        raise PermissionError("denied pending input")

    monkeypatch.setattr(reliable_memory, "read_runtime_bytes", denied)
    with pytest.raises(PermissionError, match="denied pending input"):
        capture_adoption._complete_one_pending(queue, coordinator, tmp_path, record)
    assert calls == [tmp_path / half["pending"]]


def test_stale_breadcrumb_snapshot_keeps_complete_v2_parts_and_one_task(tmp_path, monkeypatch):
    import capture_adoption

    from tests.test_breadcrumb_storage import _bundle, _publish

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    original_mark = queue.mark_capture_intent_ready
    snapshots = []

    def mark(**kwargs):
        snapshots.extend(queue.pending_capture_intents(1))
        return original_mark(**kwargs)

    monkeypatch.setattr(queue, "mark_capture_intent_ready", mark)
    publication = _publish(queue, coordinator, {"prompt": "complete v2 input"})
    monkeypatch.setattr(queue, "mark_capture_intent_ready", original_mark)
    original = dict(_links(queue)[0])
    before = _bundle(tmp_path, publication.intent_id)
    result = capture_adoption._complete_one_pending(queue, coordinator, tmp_path, snapshots[0])
    assert result == publication.intent_id
    assert _bundle(tmp_path, publication.intent_id) == before
    assert [dict(row) for row in _links(queue)] == [original]
