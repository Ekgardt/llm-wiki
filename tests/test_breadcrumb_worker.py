"""The real queue worker delivers and retries a breadcrumb without a model."""
from __future__ import annotations

import json
from functools import partial

import breadcrumb_evidence
import flush_memory
import llm_client
import pytest

from tests.test_breadcrumb_storage import _bundle, _publish
from tests.test_capture_terminal import _expire_capture_lease
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _no_model(*args, **kwargs):
    raise AssertionError("breadcrumb delivery attempted a model call")


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    publication = _publish(queue, coordinator, {"prompt": "complete original-day evidence"})
    work = partial(
        flush_memory.run_capture_worker_once, queue, coordinator,
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
        handler_versions=(2,),
    )
    return queue, coordinator, publication, work


def _assert_complete(delivery):
    queue, _coordinator_value, publication, _work = delivery
    relative = f"run/queue-results/capture-{publication.intent_id}.json"
    terminal = json.loads((queue.state_root / relative).read_bytes())
    bundle = _bundle(queue.state_root, publication.intent_id)
    head = breadcrumb_evidence.source_path(bundle.anchor)
    assert breadcrumb_evidence.read_permanent_source(queue.vault, head) == bundle.content
    journal = queue.vault / "knowledge/daily/2026-09-29.md"
    assert journal.read_bytes().count(b"<!-- llm-wiki-operation:") == 1
    assert b"complete original-day evidence" in journal.read_bytes()
    assert queue.get(terminal["processing_binding"]["task_id"]).state == "succeeded"
    return terminal


def test_worker_commits_complete_evidence_without_a_model(delivery):
    assert delivery[-1]()
    assert delivery[-1]() is None
    _assert_complete(delivery)


@pytest.mark.parametrize("boundary", ["receipt", "source", "journal", "terminal-file"])
def test_worker_retries_each_committed_boundary_without_duplicate_journal(delivery, monkeypatch, boundary):
    queue = delivery[0]
    points = {
        "receipt": (flush_memory, "_index_capture_decision"),
        "source": (breadcrumb_evidence, "publish_source"),
        "journal": (flush_memory, "_commit_capture_markdown"),
        "terminal-file": (queue, "complete_capture_terminal"),
    }
    target, name = points[boundary]
    original = getattr(target, name)

    def interrupted(*args, **kwargs):
        if boundary != "terminal-file":
            original(*args, **kwargs)
        raise OSError("delivery process interrupted")

    monkeypatch.setattr(target, name, interrupted)
    with pytest.raises(OSError, match="interrupted"):
        delivery[-1]()
    monkeypatch.setattr(target, name, original)
    with queue.connection() as database:
        task_id = database.execute("SELECT id FROM tasks").fetchone()[0]
    _expire_capture_lease(queue, task_id)

    assert delivery[-1]()
    _assert_complete(delivery)


def test_worker_refuses_missing_complete_part_and_keeps_remaining_evidence(delivery):
    queue, _coordinator_value, publication, work = delivery
    part = next((queue.state_root / "run/capture-intents").rglob("*.part"))
    part.unlink()

    with pytest.raises(FileNotFoundError):
        work()

    assert not (queue.vault / "knowledge/daily/2026-09-29.md").exists()
    assert not (queue.state_root / f"run/queue-results/capture-{publication.intent_id}.json").exists()
    assert list((queue.state_root / "run/capture-intents").rglob("*.anchor"))


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    import integration_adapter as adapter
    import markdown_transaction
    import memory_queue
    import user_prompt_capture

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    monkeypatch.setattr(adapter, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_queue, "active_memory_queue", lambda *args: queue)
    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", lambda *args: coordinator)
    monkeypatch.setattr(adapter, "spawn_detached", lambda *args: None)
    monkeypatch.setattr(adapter, "_project_context", lambda event: ("fixture", tmp_path))
    monkeypatch.setattr(adapter, "_run_delegate", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_observe_checkpoint_fail_open", lambda event: None)
    counter_state = {}
    monkeypatch.setattr(user_prompt_capture, "update_state", lambda mutate, **kwargs: mutate(counter_state))
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    return adapter, queue, coordinator


def _ingress_event(adapter, kind, **extra):
    payloads = {
        "user_prompt": {"prompt": "да"},
        "post_tool_use": {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    }
    return adapter.normalize_occurrence_event("claude", kind, {
        "session_id": "host-session", "cwd": "/fixture", "event_id": "host-event",
        "timestamp": "2026-09-29T23:59:59+00:00", **payloads[kind], **extra,
    })


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_ingress_persists_before_checkpoint_and_survives_worker_launch_failure(ingress, monkeypatch, kind):
    adapter, queue, coordinator = ingress
    observed = []

    def observe(event):
        ready = list((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
        assert len(ready) == 1
        observed.append(event.event_id)

    monkeypatch.setattr(adapter, "_observe_checkpoint_fail_open", observe)
    event = _ingress_event(adapter, kind)
    result = adapter.ingest_event(event)
    identity = result["capture_intent_ids"][0]

    assert observed == [event.event_id]
    assert result["capture_durable"] is True
    assert result["flush_spawned"] is False
    content = json.loads(_bundle(queue.state_root, identity).content)
    assert content["payload"] == event.to_dict()["payload"]
    work = partial(flush_memory.run_capture_worker_once, queue, coordinator,
                   process_missing=partial(flush_memory.process_new_capture, queue, coordinator), handler_versions=(1, 2))
    assert work() is not None
    assert work() is None
    assert (queue.vault / "knowledge/daily/2026-09-29.md").is_file()


def test_ingress_replay_with_a_host_id_reuses_the_first_acceptance(ingress):
    adapter, queue, _coordinator_value = ingress
    raw = {"session_id": "s1", "event_id": "host-once", "prompt": "same evidence"}
    first = adapter.ingest_event(adapter.normalize_occurrence_event("claude", "user_prompt", raw))
    replay = adapter.ingest_event(adapter.normalize_occurrence_event("claude", "user_prompt", raw))

    assert first["capture_intent_ids"] == replay["capture_intent_ids"]
    assert queue.claim_capture("first", handler_versions=(2,)) is not None
    assert queue.claim_capture("replay", handler_versions=(2,)) is None


def test_ingress_refuses_conflicting_host_identity_without_overwriting_evidence(ingress):
    adapter, queue, _coordinator_value = ingress
    first = adapter.ingest_event(_ingress_event(adapter, "user_prompt"))
    identity = first["capture_intent_ids"][0]
    before = _bundle(queue.state_root, identity).content

    with pytest.raises(ValueError, match="conflicts"):
        adapter.ingest_event(_ingress_event(adapter, "user_prompt", prompt="changed input"))
    assert _bundle(queue.state_root, identity).content == before


def test_auxiliary_failure_after_acceptance_is_not_reported_as_lost_capture(ingress, monkeypatch):
    adapter, queue, _coordinator_value = ingress

    def unavailable(event):
        raise OSError("project state unavailable")

    monkeypatch.setattr(adapter, "_project_context", unavailable)
    result = adapter.ingest_event(_ingress_event(adapter, "user_prompt"))

    assert result["capture_durable"] is True
    assert "project state unavailable" in result["post_capture_error"]
    assert _bundle(queue.state_root, result["capture_intent_ids"][0]).content


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_direct_hooks_use_the_same_durable_ingress(ingress, kind):
    import post_tool_capture
    import user_prompt_capture

    _adapter, queue, _coordinator_value = ingress
    hook = {"session_id": "direct", "cwd": "/fixture", "prompt": "да",
            "tool_name": "Bash", "tool_input": {"command": "pwd"}}
    calls = {"user_prompt": partial(user_prompt_capture._record_prompt, hook, hook["prompt"]),
             "post_tool_use": partial(post_tool_capture._capture_tool, hook)}
    calls[kind]()

    assert queue.claim_capture("direct", handler_versions=(2,)) is not None


def test_prompt_advisory_is_emitted_only_after_durable_acceptance(ingress, monkeypatch, capsys):
    import user_prompt_capture

    adapter, queue, _coordinator_value = ingress
    monkeypatch.setattr(user_prompt_capture, "_increment_prompt_count", lambda *args: 10)

    def advisory():
        assert queue.claim_capture("advisory", handler_versions=(2,)) is not None
        return "safe context"

    monkeypatch.setattr(user_prompt_capture, "_build_advisory_refresh", advisory)
    adapter.ingest_event(_ingress_event(adapter, "user_prompt"))
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit", "additionalContext": "safe context",
    }


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_direct_tool_wrapper_preserves_canonical_target_for_each_agent(ingress, agent):
    import post_tool_capture

    _adapter, queue, _coordinator_value = ingress
    post_tool_capture._capture_tool({
        "agent": agent, "session_id": "direct", "event_id": "canonical-target",
        "tool_name": "Edit", "tool_input": {"filePath": "src/auth.py"},
    })
    manifest = next((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
    identity = json.loads(manifest.read_bytes())["intent_id"]
    payload = json.loads(_bundle(queue.state_root, identity).content)["payload"]
    assert payload["target"] == "src/auth.py"
