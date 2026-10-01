"""A new diagnostic kind cannot erase previously counted failures."""
import json
import sqlite3

import capture_diagnostics
import memory_state


def _isolated_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", tmp_path / "failures.jsonl")


def _record_distinct_losses(count):
    for index in range(count):
        capture_diagnostics.record_capture_failure(f"capture_stage_{index}", "disk full")


def test_the_33rd_failure_kind_keeps_every_loss_after_real_state_reload(monkeypatch, tmp_path):
    _isolated_diagnostics(monkeypatch, tmp_path)
    _record_distinct_losses(33)
    state = memory_state.load_state()
    assert capture_diagnostics.capture_failure_totals(state) == {
        f"capture_stage_{index}": 1 for index in range(33)
    }
    assert "33 capture(s) lost" in capture_diagnostics.capture_failure_line(state)
    assert (tmp_path / "state.json").stat().st_size < memory_state.MAX_STATE_TARGET_BYTES


def test_a_new_kind_does_not_erase_operational_or_deferred_totals(monkeypatch, tmp_path):
    _isolated_diagnostics(monkeypatch, tmp_path)
    capture_diagnostics.record_capture_failure("mcp_tool", "tool failed")
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = 5  # SQLITE_BUSY's primary result code, also on Python 3.10.
    capture_diagnostics.record_capture_failure("adapter_post_tool_use", "writer busy", error=busy)
    _record_distinct_losses(33)
    state = memory_state.load_state()
    assert capture_diagnostics.operational_failure_totals(state) == {"mcp_tool": 1}
    assert capture_diagnostics.capture_deferred_totals(state) == {"adapter_post_tool_use": 1}
    assert sum(capture_diagnostics.capture_failure_totals(state).values()) == 33


def test_byte_pressure_evicts_cache_without_erasing_failure_counters():
    counters = {"historical_loss": {"count": 41, "last_at": "2026-09-29T00:00:00"}}
    state = {
        "capture_failures": counters,
        "project_checkpoint_reducers": {"large": {"cache": "x" * memory_state.MAX_STATE_TARGET_BYTES}},
    }
    assert memory_state.trim_state_to_budget(state) == 1
    assert state["capture_failures"] == counters
    assert capture_diagnostics.capture_failure_totals(state) == {"historical_loss": 41}
    assert len(json.dumps(state).encode()) < memory_state.MAX_STATE_TARGET_BYTES
