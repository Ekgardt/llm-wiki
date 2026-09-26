"""A refused attempt stays quarantined through the nightly prunes, so its retry advances.

docs/research/2026-09-26-a-spent-attempt-keeps-its-state.md
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import reclaim_runtime_state
from markdown_transaction import MarkdownCoordinator

from tests.test_every_store_has_a_bound import _append


def _quarantine_all(coordinator: MarkdownCoordinator) -> None:
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute("""UPDATE "transaction" SET state = 'quarantined', error_code = 'precondition_failed'""")
        database.execute("UPDATE operation SET applied = 0")


def test_the_nightly_leaves_a_refusal_quarantined_and_its_retry_advances(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    _append(coordinator, "2026-01-01", family="compile")
    _quarantine_all(coordinator)
    monkeypatch.setattr(reclaim_runtime_state, "ROOT", tmp_path / "vault")
    monkeypatch.setattr(reclaim_runtime_state, "STATE_ROOT", tmp_path / "state")

    # The snapshot writes outside the vault and the co-activation table is derived;
    # neither touches transaction state.
    monkeypatch.setattr(reclaim_runtime_state, "snapshot_memory", lambda: {"status": "skipped", "commit": None})
    monkeypatch.setattr(reclaim_runtime_state, "rebuild_co_activation", dict)

    reclaim_runtime_state.reclaim(30)

    candidate, parent = coordinator.attempt_operation_id("compile:2026-01-01")
    assert (candidate.endswith("#2"), parent is not None) == (True, True)
