"""Helpers shared by test_a_corrupt_state_is_not_replaced_by_an_empty_one.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    return directory
