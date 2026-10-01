"""A new submission of the same words is not an old capture's redelivery."""
from datetime import datetime, timedelta

import daily_log_append
import integration_adapter
import pytest
import user_prompt_capture
from capture_operation import claim_operation


def _prompt(source, **extra):
    raw = {"session_id": "s1", "sessionID": "s1", "cwd": "/work/demo",
           "prompt": "Continue the local repair", **extra}
    event = integration_adapter.normalize_occurrence_event(source, "user_prompt", raw)
    hook = integration_adapter._canonical_capture_payload(event)
    return user_prompt_capture._prompt_envelope(hook, hook["prompt"], "demo")


@pytest.mark.parametrize("source", ["claude", "codex", "opencode"])
def test_new_submissions_have_distinct_identity_but_the_same_rate_limit_key(source):
    first, second = _prompt(source), _prompt(source)
    assert (first.source_event_id != second.source_event_id,
            first.content_hash == second.content_hash) == (True, True)


@pytest.mark.parametrize("source", ["claude", "codex", "opencode"])
def test_explicit_redelivery_keeps_its_identity(source):
    first = _prompt(source, event_id="host-event-1")
    retry = _prompt(source, event_id="host-event-1")
    other = _prompt(source, event_id="host-event-2")
    assert (first.source_event_id == retry.source_event_id,
            first.source_event_id != other.source_event_id) == (True, True)


def _claim(state, event, now):
    return claim_operation(
        lambda mutate: mutate(state), namespace="prompt_capture_dedupe",
        key=f"demo::{event.content_hash[:12]}", prefix="user-prompt",
        source_event_id=event.source_event_id, rate_limit_seconds=30,
        max_entries=100, now=now,
    )


def test_same_prompt_two_days_later_reaches_a_new_daily_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    first_time = datetime(2026, 9, 28, 22, 50, 18)
    second_time = first_time + timedelta(days=2)
    state = {}
    first = _claim(state, _prompt("codex"), first_time)
    monkeypatch.setattr(daily_log_append, "local_now", lambda: first_time)
    before = daily_log_append.append_daily("demo", "s1", "first submission", first)
    second = _claim(state, _prompt("codex"), second_time)
    monkeypatch.setattr(daily_log_append, "local_now", lambda: second_time)
    after = daily_log_append.append_daily("demo", "s1", "second submission", second)
    assert ("first submission" in before.read_text(),
            "second submission" in after.read_text(), first != second) == (True, True, True)
