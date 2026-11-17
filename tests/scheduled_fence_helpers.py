"""Helpers shared by test_scheduled_fence.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


def _state(tmp_path: Path, monkeypatch) -> Path:
    import memory_state
    import scheduled_nightly

    state_dir = tmp_path / "state" / "run"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    return state_dir / "state.json"
