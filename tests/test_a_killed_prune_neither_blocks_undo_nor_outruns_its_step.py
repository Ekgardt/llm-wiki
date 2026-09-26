"""An interrupted image prune does not refuse undo, and the prune ends before its step is killed.

Audit 2026-09-26 C-12. docs/research/2026-09-26-a-killed-prune-neither-blocks-undo-nor-outruns-its-step.md
"""
from __future__ import annotations

import time
from pathlib import Path

import reclaim_runtime_state
import scheduled_nightly

from tests.test_nothing_half_written_is_left_behind import (
    _PAGE,
    _committed,
    _coordinator,
    _kill_a_prune,
)


def test_undo_puts_back_the_images_a_killed_prune_staged_aside(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    _kill_a_prune(coordinator, transaction_id)

    undo = coordinator.undo(transaction_id)
    coordinator.apply(undo.id)

    assert not (coordinator.vault / _PAGE).exists()


def test_the_image_prune_stops_at_its_deadline_and_says_so(tmp_path: Path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)
    _committed(coordinator)
    monkeypatch.setattr(reclaim_runtime_state, "ROOT", coordinator.vault)
    monkeypatch.setattr(reclaim_runtime_state, "STATE_ROOT", tmp_path / "state")

    result = reclaim_runtime_state.prune_settled_transactions(deadline=time.monotonic() - 1)

    assert result == {"pruned": 0, "failed": 0, "unfinished": True}
    assert "stopped at its deadline" in reclaim_runtime_state._unfinished_note(result)


def test_the_nightly_kills_the_step_no_sooner_than_the_prune_stops() -> None:
    step = scheduled_nightly._reclaim_step()

    assert step.timeout == reclaim_runtime_state.RECLAIM_STEP_SECONDS > reclaim_runtime_state.RECLAIM_MARGIN_SECONDS
