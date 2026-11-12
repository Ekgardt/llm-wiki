"""The real queue worker delivers and retries a breadcrumb without a model."""
from __future__ import annotations

import json
from functools import partial

import breadcrumb_evidence
import capture_intent_records
import flush_memory
import integration_adapter
import pytest
from llm_wiki import event_project

from tests.breadcrumb_storage_helpers import _bundle
from tests.breadcrumb_worker_helpers import (
    _assert_complete,
    _ingress_event,
    delivery,  # noqa: F401
    ingress,  # noqa: F401
)
from tests.capture_terminal_helpers import _expire_capture_lease


def test_worker_commits_complete_evidence_without_a_model(delivery):  # noqa: F811
    assert delivery[-1]()
    assert delivery[-1]() is None
    _assert_complete(delivery)


@pytest.mark.parametrize("boundary", ["receipt", "source", "journal", "terminal-file"])
def test_worker_retries_each_committed_boundary_without_duplicate_journal(delivery, monkeypatch, boundary):  # noqa: F811
    queue = delivery[0]
    points = {
        "receipt": (capture_intent_records, "index_capture_decision"),
        "source": (breadcrumb_evidence, "publish_source"),
        "journal": (capture_intent_records, "commit_capture_markdown"),
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


def test_worker_refuses_missing_complete_part_and_keeps_remaining_evidence(delivery):  # noqa: F811
    queue, _coordinator_value, publication, work = delivery
    part = next((queue.state_root / "run/capture-intents").rglob("*.part"))
    part.unlink()

    with pytest.raises(FileNotFoundError):
        work()

    assert not (queue.vault / "knowledge/daily/2026-09-29.md").exists()
    assert not (queue.state_root / f"run/queue-results/capture-{publication.intent_id}.json").exists()
    assert list((queue.state_root / "run/capture-intents").rglob("*.anchor"))


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_ingress_persists_before_checkpoint_and_survives_worker_launch_failure(ingress, monkeypatch, kind):  # noqa: F811
    import project_checkpoint
    queue, coordinator = ingress
    observed = []

    def observe(event):
        ready = list((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
        assert len(ready) == 1
        observed.append(event.event_id)

    monkeypatch.setattr(project_checkpoint, "observe_project_checkpoint", observe)
    event = _ingress_event(kind)
    result = integration_adapter.ingest_event(event)
    identity = result["capture_intent_ids"][0]

    assert observed == []
    assert result["capture_durable"] is True
    assert result["flush_spawned"] is False
    content = json.loads(_bundle(queue.state_root, identity).content)
    assert content["payload"] == event.to_dict()["payload"]
    work = partial(flush_memory.run_capture_worker_once, queue, coordinator,
                   process_missing=partial(flush_memory.process_new_capture, queue, coordinator), handler_versions=(1, 2))
    assert work() is not None
    assert observed == [event.event_id]
    assert work() is None
    assert (queue.vault / "knowledge/daily/2026-09-29.md").is_file()


def test_ingress_replay_with_a_host_id_reuses_the_first_acceptance(ingress):  # noqa: F811
    queue, _coordinator_value = ingress
    raw = {"session_id": "s1", "event_id": "host-once", "prompt": "same evidence"}
    first = integration_adapter.ingest_event(integration_adapter.normalize_occurrence_event("claude", "user_prompt", raw))
    replay = integration_adapter.ingest_event(integration_adapter.normalize_occurrence_event("claude", "user_prompt", raw))

    assert first["capture_intent_ids"] == replay["capture_intent_ids"]
    assert queue.claim_capture("first", handler_versions=(2,)) is not None
    assert queue.claim_capture("replay", handler_versions=(2,)) is None


def test_ingress_refuses_conflicting_host_identity_without_overwriting_evidence(ingress):  # noqa: F811
    queue, _coordinator_value = ingress
    first = integration_adapter.ingest_event(_ingress_event("user_prompt"))
    identity = first["capture_intent_ids"][0]
    before = _bundle(queue.state_root, identity).content

    with pytest.raises(ValueError, match="conflicts"):
        integration_adapter.ingest_event(_ingress_event("user_prompt", prompt="changed input"))
    assert _bundle(queue.state_root, identity).content == before


def test_auxiliary_failure_after_acceptance_is_not_reported_as_lost_capture(ingress, monkeypatch):  # noqa: F811
    queue, _coordinator_value = ingress

    def unavailable(event):
        raise OSError("project state unavailable")

    monkeypatch.setattr(event_project, "project_context", unavailable)
    result = integration_adapter.ingest_event(_ingress_event("user_prompt"))

    assert result["capture_durable"] is True
    assert "project state unavailable" in result["post_capture_error"]
    assert _bundle(queue.state_root, result["capture_intent_ids"][0]).content


@pytest.mark.parametrize("kind", ["user_prompt", "post_tool_use"])
def test_direct_hooks_use_the_same_durable_ingress(ingress, kind):  # noqa: F811
    import post_tool_capture
    import user_prompt_capture

    queue, _coordinator_value = ingress
    hook = {"session_id": "direct", "cwd": "/fixture", "prompt": "да",
            "tool_name": "Bash", "tool_input": {"command": "pwd"}}
    calls = {"user_prompt": partial(user_prompt_capture.record_prompt, hook, hook["prompt"]),
             "post_tool_use": partial(post_tool_capture.capture_tool, hook)}
    calls[kind]()

    assert queue.claim_capture("direct", handler_versions=(2,)) is not None


def test_prompt_advisory_is_emitted_only_after_durable_acceptance(ingress, monkeypatch, capsys):  # noqa: F811
    import prompt_capture_followup

    queue, _coordinator_value = ingress
    monkeypatch.setattr(prompt_capture_followup, "increment_prompt_count", lambda *args: 10)

    def advisory():
        assert queue.claim_capture("advisory", handler_versions=(2,)) is not None
        return "safe context"

    monkeypatch.setattr(prompt_capture_followup, "advisory_refresh_text", advisory)
    integration_adapter.ingest_event(_ingress_event("user_prompt"))
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit", "additionalContext": "safe context",
    }


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_direct_tool_wrapper_preserves_canonical_target_for_each_agent(ingress, agent):  # noqa: F811
    import post_tool_capture

    queue, _coordinator_value = ingress
    post_tool_capture.capture_tool({
        "agent": agent, "session_id": "direct", "event_id": "canonical-target",
        "tool_name": "Edit", "tool_input": {"filePath": "src/auth.py"},
    })
    manifest = next((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
    identity = json.loads(manifest.read_bytes())["intent_id"]
    payload = json.loads(_bundle(queue.state_root, identity).content)["payload"]
    assert payload["target"] == "src/auth.py"
