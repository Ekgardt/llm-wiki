"""An interrupted prune is healed by recovery and named, not read as damage (audit 2026-09-26 B-22).

docs/research/2026-09-26-an-interrupted-prune-is-repaired.md
"""
from __future__ import annotations

from pathlib import Path

import doctor

from tests.test_nothing_half_written_is_left_behind import _committed, _coordinator, _kill_a_prune


def test_doctor_does_not_call_a_staged_prune_unsafe(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    _kill_a_prune(coordinator, _committed(coordinator))

    _identifiers, unsafe = doctor._transaction_artifacts(tmp_path / "state", float("inf"))

    assert unsafe is False


def test_recovery_settles_the_interrupted_prune(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)

    coordinator.recover()

    assert (staged.exists(), (coordinator.transaction_root / transaction_id).exists()) == (False, True)
