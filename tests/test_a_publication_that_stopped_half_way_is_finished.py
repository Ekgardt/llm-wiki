"""A publisher killed before it marked an intent ready must not lose the session.

See `docs/research/2026-09-17-a-publication-that-stopped-half-way-is-finished.md`.
"""

from __future__ import annotations

from reliable_memory import canonical_json_bytes

from tests.a_publication_that_stopped_half_way_is_finished_helpers import (
    LATER,
    _complete,
    _pending_ids,
    _publish_pending_intent,
)
from tests.capture_intent_adoption_helpers import (
    _coordinator,
    _linked_ids,
    _queue,
    _skipped_ids,
)


def test_the_half_published_intent_this_recovers_from_is_real(tmp_path, monkeypatch):
    """Guard the premise: the ready-state sweeper cannot see a pending row."""
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"pending-premise")

    orphans = queue.ready_capture_intents_without_task(32)

    assert (_pending_ids(queue), orphans, (tmp_path / half["pending"]).exists()) == (
        [half["intent_id"]],
        [],
        True,
    )


def test_a_pending_intent_is_published_and_given_a_task(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
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
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"still-running")

    result = _complete(queue, coordinator, tmp_path)

    assert (result["examined"], _pending_ids(queue)) == (0, [half["intent_id"]])


def test_a_pending_record_whose_bytes_moved_is_a_named_skip(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"tampered")
    target = tmp_path / half["pending"]
    target.chmod(0o600)
    target.write_bytes(canonical_json_bytes({"seed": "tampered-with"}))

    result = _complete(queue, coordinator, tmp_path, now=LATER)

    assert (_skipped_ids(result), result["completed"]) == ([half["intent_id"]], [])
