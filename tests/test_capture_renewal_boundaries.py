"""Slow publication and existing-terminal verification must retain live authority."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from functools import partial

import breadcrumb_storage
import flush_memory
import integration_adapter
import pytest

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_breadcrumb_storage import _publish
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _renewal_signal(coordinator, monkeypatch):
    renewed = threading.Event()
    heartbeat = coordinator.heartbeat_intent_fence

    def observed(fence, owner):
        heartbeat(fence, owner)
        renewed.set()

    monkeypatch.setattr(coordinator, "heartbeat_intent_fence", observed)
    return renewed


def test_existing_terminal_read_is_inside_worker_renewal(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _publish(queue, coordinator)
    renewed = _renewal_signal(coordinator, monkeypatch)
    monkeypatch.setattr(flush_memory, "CAPTURE_KEEPALIVE_SECONDS", 0.01)
    complete = queue.complete_existing_capture_terminal

    def held_read(*args, **kwargs):
        assert renewed.wait(timeout=SHORT_TIMEOUT), "no renewal while reading a terminal"
        return complete(*args, **kwargs)

    monkeypatch.setattr(queue, "complete_existing_capture_terminal", held_read)
    assert flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )


def test_storing_parts_is_inside_publication_renewal(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    renewed = _renewal_signal(coordinator, monkeypatch)
    # The test accelerates the scheduling interval, never lease expiry or fencing.
    monkeypatch.setattr(integration_adapter, "_publication_heartbeat_interval", lambda owner: 0.01)
    store = breadcrumb_storage._store_parts

    def held_write(*args, **kwargs):
        assert renewed.wait(timeout=SHORT_TIMEOUT), "no renewal while writing parts"
        store(*args, **kwargs)

    monkeypatch.setattr(breadcrumb_storage, "_store_parts", held_write)
    assert _publish(queue, coordinator).registered


def _intent_expiry(coordinator, intent_id):
    with coordinator._connect() as database:
        value = database.execute(
            "SELECT expires_at FROM intent_fences WHERE intent_id=? AND mode='capture'",
            (intent_id,),
        ).fetchone()[0]
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_real_publication_outlives_its_original_lease(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    store = breadcrumb_storage._store_parts

    def slow_storage(state_root, intent_id, parts):
        original = _intent_expiry(coordinator, intent_id)
        while datetime.now(timezone.utc) < original:
            remaining = (original - datetime.now(timezone.utc)).total_seconds()
            threading.Event().wait(max(0.0, remaining))  # The recorded lease boundary, not a synchronization guess.
        assert datetime.now(timezone.utc) >= original
        assert _intent_expiry(coordinator, intent_id) > original
        store(state_root, intent_id, parts)

    monkeypatch.setattr(breadcrumb_storage, "_store_parts", slow_storage)
    assert _publish(queue, coordinator).registered


def test_expired_publication_authority_cannot_report_acceptance(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    store = breadcrumb_storage._store_parts

    def expired_storage(state_root, intent_id, parts):
        store(state_root, intent_id, parts)
        with coordinator._connect() as database:
            database.execute(
                "UPDATE intent_fences SET expires_at='2000-01-01T00:00:00Z' WHERE intent_id=?",
                (intent_id,),
            )

    monkeypatch.setattr(breadcrumb_storage, "_store_parts", expired_storage)
    with pytest.raises(RuntimeError, match="intent_fence_lost"):
        _publish(queue, coordinator)
    assert list((tmp_path / "run/capture-intents").rglob("*.part"))
    monkeypatch.setattr(breadcrumb_storage, "_store_parts", store)
    assert _publish(queue, coordinator).registered
