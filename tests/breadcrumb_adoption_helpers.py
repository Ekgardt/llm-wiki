"""Helpers shared by test_breadcrumb_adoption.py and the test files importing them."""
from __future__ import annotations

from tests.breadcrumb_storage_helpers import _publish, _registration_failure
from tests.queue_v3_capture_links_helpers import _coordinator, _queue


def _orphan(root, monkeypatch, stage):
    queue, coordinator = _queue(root), _coordinator(root)
    with monkeypatch.context() as patch:
        patch.setattr(queue, stage, _registration_failure)
        publication = _publish(queue, coordinator)
    assert publication.registered is False
    return queue, coordinator, publication.intent_id
