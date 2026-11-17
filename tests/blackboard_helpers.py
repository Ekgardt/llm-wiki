"""Helpers shared by test_blackboard.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.reliability_v3_adoption_helpers import _vault, build_adopted_reliability_v3


@pytest.fixture
def blackboard_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    vault, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state_root)
    projects = vault / "knowledge/projects"
    (projects / "demo/.blackboard").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return vault, state_root
