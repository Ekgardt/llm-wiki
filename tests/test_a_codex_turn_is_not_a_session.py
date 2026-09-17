"""The end of a Codex turn captures its session at most once per window.

See `docs/research/2026-09-17-a-codex-turn-is-not-a-session.md`.
"""
from __future__ import annotations

import io
import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tests.adopted_capture_vault import (  # noqa: E402
    adopted_capture_vault,
    host_transcript,
    intent_records,
)

START = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def codex(tmp_path, monkeypatch):
    """(codex_memory, state root, project) on an adopted vault with its own state file."""
    import capture_diagnostics
    import codex_memory
    import integration_adapter
    import memory_state

    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    run = tmp_path / "turn-end-state"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", run / "capture-failures.jsonl")
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 1)
    monkeypatch.setattr(integration_adapter, "_run_delegate", lambda *_a, **_k: Namespace(returncode=0))
    return codex_memory, state_root, project


def _stop(project: Path, transcript: Path, session: str, turn: str) -> dict:
    return {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(project),
        "hook_event_name": "Stop",
        "model": "gpt-test",
        "turn_id": turn,
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "done",
    }


def _turn(codex, session: str, turn: str, minutes: int, name: str = "session.jsonl") -> None:
    module, state_root, project = codex
    transcript = host_transcript(state_root, name, f"the session so far, up to {turn}\n")
    now = START + timedelta(minutes=minutes)
    _result, tail = module._ingest_codex_hook(_stop(project, transcript, session, turn), now)
    module._capture_quiet_tail(tail)


def _captured_turns(codex) -> list[tuple]:
    records = intent_records(codex[1])
    return sorted((record["session"], record["source_event_id"]) for record in records)


def test_three_turns_in_ten_minutes_are_one_capture(codex):
    for index, minutes in enumerate((0, 5, 10)):
        _turn(codex, "session-a", f"turn-{index}", minutes)

    assert _captured_turns(codex) == [("session-a", "turn-0")]


def test_a_turn_after_the_window_captures_the_session_again(codex):
    _turn(codex, "session-a", "turn-0", 0)
    _turn(codex, "session-a", "turn-1", 31)

    assert _captured_turns(codex) == [("session-a", "turn-0"), ("session-a", "turn-1")]


def test_the_quiet_end_of_one_session_is_captured_by_the_next_codex_hook(codex):
    _turn(codex, "session-a", "turn-0", 0)
    _turn(codex, "session-a", "turn-1", 5)
    _turn(codex, "session-b", "turn-0", 40, name="other.jsonl")

    assert _captured_turns(codex) == [
        ("session-a", "turn-0"),
        ("session-a", "turn-1"),
        ("session-b", "turn-0"),
    ]


def test_a_capture_that_failed_gives_its_claim_back(codex, tmp_path):
    module, _state_root, project = codex
    refused = tmp_path / "outside-the-allowed-roots.jsonl"
    refused.write_text("text\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        module._ingest_codex_hook(_stop(project, refused, "session-a", "turn-0"), START)
    _turn(codex, "session-a", "turn-1", 1)

    assert _captured_turns(codex) == [("session-a", "turn-1")]


def test_a_failed_codex_hook_is_written_to_the_failure_trail(codex, tmp_path, monkeypatch, capsys):
    import capture_diagnostics

    module, _state_root, project = codex
    refused = tmp_path / "outside-the-allowed-roots.jsonl"
    refused.write_text("text\n", encoding="utf-8")
    payload = json.dumps(_stop(project, refused, "session-a", "turn-0"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    module.command_hook(Namespace())

    trail = capture_diagnostics.FAILURE_LOG.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in trail] == ["codex_hook"]
    assert json.loads(capsys.readouterr().out) == {}
