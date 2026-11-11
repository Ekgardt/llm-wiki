"""Helpers shared by test_a_codex_turn_is_not_a_session.py and the test files importing them."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import memory_state
import pytest
from llm_wiki import integration_delegates

from tests.adopted_capture_vault import adopted_capture_vault


@pytest.fixture
def codex(tmp_path, monkeypatch):
    """(codex_memory, state root, project) on an adopted vault with its own state file."""
    import codex_memory

    state_root, project = adopted_capture_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda _args: 1)
    monkeypatch.setattr(integration_delegates, "run_delegate", lambda *_a, **_k: Namespace(returncode=0))
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
