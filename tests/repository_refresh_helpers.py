"""Helpers shared by test_repository_refresh.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _isolated_reader_cache():
    import evidence_reader_cache

    evidence_reader_cache.clear()
    yield
    evidence_reader_cache.clear()


@pytest.fixture
def adopted_vault(tmp_path, monkeypatch):
    """A vault whose coordinator went through real V3 adoption, so a fence exists."""
    from installed_memory_repair import repair_installed_vault

    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS / "integration_adapter.py").read_bytes()
    )
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    report = repair_installed_vault(
        root=root, state_root=state, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    assert report["overall_status"] == "ok"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))

    return root, state
