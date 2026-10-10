"""A capture step that fails leaves a record instead of nothing (audit 2026-09-27 C-3).

docs/research/2026-09-27-a-capture-that-fails-says-so.md
"""
from __future__ import annotations

import json
from pathlib import Path

import capture_diagnostics
import integration_adapter
import memory_state
import pytest
import user_prompt_capture


@pytest.fixture
def trail(tmp_path: Path, monkeypatch) -> Path:
    run = tmp_path / "diagnostics"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", run / "capture-failures.jsonl")
    return run / "capture-failures.jsonl"


def _kinds(trail: Path) -> list[str]:
    return [json.loads(line)["kind"] for line in trail.read_text("utf-8").splitlines()]


def test_a_malformed_hook_input_is_a_recorded_loss(trail: Path) -> None:
    parsed = [capture_diagnostics.hook_object(raw, "tool_input") for raw in ("{not json", "[1, 2]", "")]

    assert (parsed, _kinds(trail)) == ([{}, {}, {}], ["tool_input", "tool_input"])


def test_a_prompt_counter_failure_reaches_the_accepted_followup_reporter(trail: Path, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise TimeoutError("state lock busy")

    monkeypatch.setattr(user_prompt_capture, "update_state", refuse)

    with pytest.raises(TimeoutError, match="state lock busy"):
        user_prompt_capture._increment_prompt_count("s1", "demo")
    # Accepted content is already durable. The caller reports the auxiliary
    # failure; counting it as a lost event contradicts durable acceptance.
    assert not trail.exists()


def _hook_errors(state_root: Path) -> list[str]:
    return (state_root / "logs" / "hook-errors.log").read_text("utf-8").splitlines()


def test_a_session_start_drain_that_fails_is_logged(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "failing.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setattr(integration_adapter, "ROOT", tmp_path)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)

    integration_adapter._run_maintenance_command("failing.py", "work")

    assert [line.split("] ", 1)[1] for line in _hook_errors(tmp_path)] == [
        "session-start maintenance: failing.py work exited 3"
    ]


def test_a_compile_that_cannot_be_started_is_logged(tmp_path: Path, monkeypatch) -> None:
    def refuse(**_kwargs) -> None:
        raise OSError("no fork")

    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(integration_adapter, "_run_maintenance_command", lambda *_args: None)
    monkeypatch.setattr(integration_adapter, "_catch_up_missed_nightly", lambda: None)
    monkeypatch.setattr(integration_adapter, "spawn_compile_if_idle", refuse)

    integration_adapter._run_session_start_maintenance()

    assert [line.split("] ", 1)[1] for line in _hook_errors(tmp_path)] == ["session-start compile: OSError: no fork"]
