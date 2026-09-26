"""The nightly command runs every capture recovery pass and names what it left (audit 2026-09-26 C-12).

docs/research/2026-09-26-the-nightly-finishes-what-a-publisher-left-half-way.md
"""
from __future__ import annotations

import inspect

import capture_adoption
import capture_diagnostics
import markdown_transaction
import memory_queue

from tests.test_a_publication_that_stopped_half_way_is_finished import _pending_ids, _publish_pending_intent
from tests.test_capture_intent_adoption import _coordinator, _queue


def _active_vault(tmp_path, monkeypatch, queue, coordinator) -> None:
    monkeypatch.setattr("integration_adapter.STATE_ROOT", tmp_path)
    monkeypatch.setattr(capture_adoption, "ROOT", tmp_path)
    monkeypatch.setattr(capture_adoption, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(memory_queue, "active_memory_queue", lambda _vault, _state: queue)
    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", lambda _vault, _state: coordinator)


def test_the_nightly_command_finishes_a_half_published_intent(tmp_path, monkeypatch) -> None:
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _active_vault(tmp_path, monkeypatch, queue, coordinator)
    half = _publish_pending_intent(tmp_path, queue, coordinator, b"nightly-pending")
    # Aged past the recovery window without waiting a minute.
    monkeypatch.setattr(capture_adoption, "PENDING_INTENT_RECOVERY_SECONDS", -3600)

    results = capture_adoption.adopt_in_active_vault()

    finished = [entry["intent_id"] for entry in results["complete_pending_capture_intents"]["completed"]]
    assert (finished, _pending_ids(queue)) == ([half["intent_id"]], [])


def test_the_nightly_command_writes_down_an_intent_it_could_not_recover(tmp_path, monkeypatch) -> None:
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    _active_vault(tmp_path, monkeypatch, queue, coordinator)
    recorded: list[str] = []
    monkeypatch.setattr(capture_diagnostics, "record_capture_failure", lambda _kind, message, **_kw: recorded.append(message))

    def stuck(*_args, **_kwargs):
        return {"examined": 1, "completed": [], "adopted": [], "skipped": [{"intent_id": "i-1", "reason": "moved", "retried": False}]}

    monkeypatch.setattr(capture_adoption, "RECOVERY_SWEEPS", (stuck,))

    capture_adoption.adopt_in_active_vault()

    assert recorded == ["1 intent(s) not adopted; first i-1: moved"]


def test_every_recovery_pass_is_run_by_both_callers() -> None:
    """A pass added to the module and left out of the list would run nowhere."""
    passes = {
        name
        for name, function in inspect.getmembers(capture_adoption, inspect.isfunction)
        if name.endswith("_capture_intents") and function.__module__ == "capture_adoption"
    }

    assert passes == {sweep.__name__ for sweep in capture_adoption.RECOVERY_SWEEPS}
