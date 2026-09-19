"""The installer adopts Reliability V3 before the runtime sync, not after it.

A real end-to-end install finished with session capture disabled: the bounded runtime sync
opens the pre-adoption coordinator, which leaves half a legacy pair, and half a pair is a
`conflict` — a state the cutover cannot adopt. Issue #17 a third time.

Research: `docs/research/2026-09-17-the-queue-is-adopted-before-anything-writes-to-it.md`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from installed_memory_repair import inspect_installed_vault, repair_installed_vault
from markdown_transaction import active_or_legacy_coordinator

ROOT = Path(__file__).resolve().parent.parent
STEPS = {
    "install.sh": ("adopt-ownership-v3", "scripts/sync_memory.py"),
    "install.ps1": ("adopt-ownership-v3", "scripts\\sync_memory.py"),
}


@pytest.mark.parametrize("name", sorted(STEPS))
def test_the_installer_adopts_before_it_synchronizes(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    adoption, sync = STEPS[name]

    positions = (text.find(adoption), text.find(sync))

    assert positions[0] > 0 and positions[1] > 0, positions
    assert positions[0] < positions[1]


@pytest.fixture
def vault() -> Path:
    """The checkout itself: adoption digests the installed integration sources, and only
    the state root is written."""
    return ROOT


def _adoption_state(vault: Path, state_root: Path) -> object:
    report = inspect_installed_vault(root=vault, state_root=state_root)
    return report["details"]["adoption_state"]


def test_a_coordinator_opened_first_leaves_a_vault_that_cannot_be_adopted(vault, tmp_path) -> None:
    """What the installer did when the sync ran before the adoption step."""
    state_root = tmp_path / "state"
    active_or_legacy_coordinator(vault, state_root)

    outcome = repair_installed_vault(
        root=vault, state_root=state_root, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )

    assert (_adoption_state(vault, state_root), outcome["overall_status"]) == ("conflict", "error")


def test_a_coordinator_opened_after_the_cutover_keeps_the_vault_adopted(vault, tmp_path) -> None:
    state_root = tmp_path / "state"

    outcome = repair_installed_vault(
        root=vault, state_root=state_root, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    active_or_legacy_coordinator(vault, state_root)

    assert (_adoption_state(vault, state_root), outcome["overall_status"]) == ("adopted", "ok")
