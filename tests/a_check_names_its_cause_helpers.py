"""Helpers shared by test_a_check_names_its_cause.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest
from installed_memory_repair import repair_installed_vault

from tests.reliability_v3_adoption_helpers import _vault


@pytest.fixture
def adopted(tmp_path: Path) -> tuple[Path, Path]:
    root, state_root = _vault(tmp_path)
    report = repair_installed_vault(
        root=root, state_root=state_root, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    assert report["details"]["adoption_state"] == "adopted"
    return root, state_root
