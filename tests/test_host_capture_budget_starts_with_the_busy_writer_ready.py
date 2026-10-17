"""The host stopwatch measures ingestion with an already busy writer."""
import integration_adapter as adapter
import pytest

import tests.test_a_prompt_checkpoint_does_not_outwait_its_host as scenario


@pytest.mark.parametrize("event", ["user_prompt", "post_tool_use"])
def test_the_busy_writer_is_ready_before_host_ingestion(tmp_path, monkeypatch, event):
    observed = {}
    hold = scenario._hold_writer
    ingest = adapter.ingest_event

    def held_writer(coordinator, held, release):
        observed["held"] = held
        return hold(coordinator, held, release)

    def checked_ingest(envelope, **kwargs):
        held = observed.get("held")
        observed["ready_at_ingest"] = held is not None and held.is_set()
        return ingest(envelope, **kwargs)

    monkeypatch.setattr(scenario, "_hold_writer", held_writer)
    monkeypatch.setattr(adapter, "ingest_event", checked_ingest)
    scenario.test_capture_keeps_its_checkpoint_pending_without_waiting_past_host(tmp_path, monkeypatch, event)
    assert observed["ready_at_ingest"], "fixture writer must be ready before timing host ingestion"
