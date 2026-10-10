"""Publication preserves complete evidence across contention and queue failure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import breadcrumb_protocol as protocol
import breadcrumb_storage as storage
import memory_queue
import pytest
from reliable_memory import canonical_json_bytes

from tests.test_capture_publication_with_busy_writer import _other_process_writer
from tests.test_queue_v3_capture_links import _coordinator, _queue

MOMENT = datetime(2026, 9, 29, tzinfo=timezone.utc)
SCOPE = {"host": "claude", "session": "test", "kind": "user_prompt", "occurrence": "one"}


def _publish(queue, coordinator, event=None, scope=None, accepted_at=MOMENT):
    return storage.publish_breadcrumb(
        queue, coordinator, SCOPE if scope is None else scope,
        {"prompt": "complete prompt"} if event is None else event,
        occurred_at=MOMENT, accepted_at=accepted_at, time_origin="host",
    )


def _bundle(root: Path, intent_id: str):
    return storage.load_bundle(root, storage._stored_manifest(root, intent_id))


def _artifacts(root: Path) -> dict[str, bytes]:
    directory = root / "run/capture-intents"
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_publication_accepts_a_full_host_boundary_while_another_writer_is_busy(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    event = {"prompt": "x" * 1_048_576}

    with _other_process_writer(tmp_path):
        publication = _publish(queue, coordinator, event)

    bundle = _bundle(tmp_path, publication.intent_id)
    lease = queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,))
    assert publication.registered is True
    assert bundle.content == canonical_json_bytes(event)
    assert len(bundle.parts) > 1
    assert lease.handler_version == storage.HANDLER_VERSION


def test_replay_keeps_first_acceptance_time_and_one_task(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    first = _publish(queue, coordinator)
    before = _artifacts(tmp_path)

    replay = _publish(queue, coordinator, accepted_at=MOMENT + timedelta(days=3))

    assert replay.intent_id == first.intent_id
    assert _artifacts(tmp_path) == before
    assert protocol.read_anchor(_bundle(tmp_path, first.intent_id).anchor)["accepted_at"] == MOMENT.isoformat()
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is not None
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is None


def test_conflicting_input_cannot_replace_a_previous_occurrence(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _publish(queue, coordinator)
    before = _artifacts(tmp_path)

    with pytest.raises(ValueError, match="conflicts"):
        _publish(queue, coordinator, {"prompt": "different input"})

    assert _artifacts(tmp_path) == before


def test_equal_prompts_from_different_occurrences_are_both_preserved(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    first = _publish(queue, coordinator)
    second = _publish(queue, coordinator, scope={**SCOPE, "occurrence": "two"})

    assert first.intent_id != second.intent_id
    assert _bundle(tmp_path, first.intent_id).content == _bundle(tmp_path, second.intent_id).content
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is not None
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is not None


def _registration_failure(*args, **kwargs):
    raise RuntimeError("queue registration unavailable")


def test_enqueue_failure_is_pending_evidence_and_can_be_registered_later(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    enqueue = queue.enqueue_capture_task_replay_safe
    monkeypatch.setattr(queue, "enqueue_capture_task_replay_safe", _registration_failure)

    publication = _publish(queue, coordinator)

    assert publication.registered is False
    assert "queue registration unavailable" in publication.registration_error
    bundle = _bundle(tmp_path, publication.intent_id)
    assert bundle.content == canonical_json_bytes({"prompt": "complete prompt"})
    assert storage.anchor_path(tmp_path, publication.intent_id).with_suffix(".json").is_file()
    monkeypatch.setattr(queue, "enqueue_capture_task_replay_safe", enqueue)
    storage.register_manifest(queue, coordinator, bundle.manifest)
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is not None


def test_a_complete_file_without_a_database_row_survives_reopening(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    index = queue.index_capture_intent_pending
    monkeypatch.setattr(queue, "index_capture_intent_pending", _registration_failure)
    publication = _publish(queue, coordinator)
    assert publication.registered is False
    monkeypatch.setattr(queue, "index_capture_intent_pending", index)
    reopened = memory_queue.MemoryQueue._from_v3_candidate(queue.db_path, state_root=tmp_path)
    bundle = _bundle(tmp_path, publication.intent_id)

    storage.register_manifest(reopened, _coordinator(tmp_path), bundle.manifest)

    assert reopened.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is not None


def _part_write_failure(*args, **kwargs):
    raise OSError("storage unavailable")


def test_partial_storage_failure_is_not_accepted_and_retry_keeps_the_anchor(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    store_parts = storage._store_parts
    monkeypatch.setattr(storage, "_store_parts", _part_write_failure)

    with pytest.raises(OSError, match="storage unavailable"):
        _publish(queue, coordinator)

    intent_id = protocol.occurrence_identity(SCOPE)
    before = storage.anchor_path(tmp_path, intent_id).read_bytes()
    assert queue.claim_capture("test", handler_versions=(storage.HANDLER_VERSION,)) is None
    monkeypatch.setattr(storage, "_store_parts", store_parts)
    publication = _publish(queue, coordinator, accepted_at=MOMENT + timedelta(days=1))
    assert publication.registered is True
    assert storage.anchor_path(tmp_path, intent_id).read_bytes() == before


def test_missing_part_refuses_reconstruction_and_reregistration(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    publication = _publish(queue, coordinator)
    manifest = storage._stored_manifest(tmp_path, publication.intent_id)
    digest = protocol.read_manifest(manifest)["last_part_sha256"]
    storage.part_path(tmp_path, publication.intent_id, digest).unlink()

    with pytest.raises(FileNotFoundError):
        storage.register_manifest(queue, coordinator, manifest)


def test_legacy_worker_cannot_claim_a_new_breadcrumb(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _publish(queue, coordinator)

    assert queue.claim_capture("legacy") is None
    assert queue.claim_capture("new", handler_versions=(1, 2)).handler_version == 2


@pytest.mark.parametrize("versions", [(), (0,), (-1,), (True,), ("2",)])
def test_invalid_handler_selection_does_not_claim_work(tmp_path, versions):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _publish(queue, coordinator)

    with pytest.raises(ValueError):
        queue.claim_capture("test", handler_versions=versions)

    assert queue.claim_capture("test", handler_versions=(2,)) is not None


def _host_occurrence(kind, identity, clock):
    from integration_adapter import normalize_occurrence_event

    raw = {"session_id": "s1", "cwd": "/fixture", **identity, **clock}
    payloads = {
        "user_prompt": {"prompt": "repeat this action"},
        "post_tool_use": {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    }
    return normalize_occurrence_event("claude", kind, {**raw, **payloads[kind]})


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
@pytest.mark.parametrize("clock", [{}, {"timestamp": MOMENT.isoformat()}])
@pytest.mark.parametrize("identity", [{}, {"event_id": ""}])
def test_unidentified_host_actions_get_distinct_occurrences(kind, clock, identity):
    first = _host_occurrence(kind, identity, clock)
    second = _host_occurrence(kind, identity, clock)

    assert first.source_event_id
    assert second.source_event_id
    assert first.source_event_id != second.source_event_id
    assert first.event_id != second.event_id
    assert first.payload["occurrence_id"] == first.source_event_id
    assert second.payload["occurrence_id"] == second.source_event_id


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_explicit_occurrence_survives_normalization_and_replay(kind):
    identity = {"event_id": "", "occurrence_id": "accepted-once"}
    first = _host_occurrence(kind, identity, {"timestamp": MOMENT.isoformat()})
    replay = _host_occurrence(kind, identity, {"timestamp": MOMENT.isoformat()})

    assert first.source_event_id == "accepted-once"
    assert first.event_id == replay.event_id


def test_blank_identity_does_not_hide_the_host_tool_identifier():
    event = _host_occurrence("post_tool_use", {"event_id": "", "tool_use_id": "call-one"}, {})

    assert event.source_event_id == "call-one"


def test_codex_prompts_in_one_turn_remain_distinct_without_occurrence_identifiers():
    from integration_adapter import normalize_occurrence_event

    raw = {"session_id": "s1", "turn_id": "turn-one", "prompt": "repeat this action"}
    first = normalize_occurrence_event("codex", "user_prompt", raw)
    second = normalize_occurrence_event("codex", "user_prompt", raw)

    assert first.source_event_id != "turn-one"
    assert second.source_event_id != first.source_event_id
    assert second.event_id != first.event_id


def test_codex_tools_without_call_identifiers_do_not_share_the_turn_identity():
    from integration_adapter import normalize_occurrence_event

    raw = {"session_id": "s1", "turn_id": "turn-one", "tool_name": "Bash", "target": "pwd"}
    first = normalize_occurrence_event("codex", "post_tool_use", raw)
    second = normalize_occurrence_event("codex", "post_tool_use", raw)

    assert first.source_event_id != second.source_event_id
    assert first.source_event_id != "turn-one"


def _publish_host_occurrence(queue, coordinator, envelope):
    scope = {
        "host": envelope.agent, "session": envelope.session,
        "kind": envelope.event_type, "worktree": envelope.worktree,
        "occurrence": envelope.source_event_id,
    }
    return storage.publish_breadcrumb(
        queue, coordinator, scope, envelope.to_dict()["payload"],
        occurred_at=envelope.occurred_at, accepted_at=envelope.captured_at,
        time_origin="host",
    )


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_two_equal_host_actions_at_one_time_survive_durable_publication(tmp_path, kind):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    first = _host_occurrence(kind, {}, {"timestamp": MOMENT.isoformat()})
    second = _host_occurrence(kind, {}, {"timestamp": MOMENT.isoformat()})

    saved = _publish_host_occurrence(queue, coordinator, first)
    another = _publish_host_occurrence(queue, coordinator, second)
    replay = _publish_host_occurrence(queue, coordinator, first)

    assert saved.intent_id != another.intent_id
    assert saved.intent_id == replay.intent_id
    assert _bundle(tmp_path, saved.intent_id).content == canonical_json_bytes(first.to_dict()["payload"])
    assert _bundle(tmp_path, another.intent_id).content == canonical_json_bytes(second.to_dict()["payload"])
    assert queue.claim_capture("first", handler_versions=(2,)) is not None
    assert queue.claim_capture("second", handler_versions=(2,)) is not None
    assert queue.claim_capture("third", handler_versions=(2,)) is None
