"""A quarantined transaction that left nothing behind is settled; empty ready shards go.

See docs/research/2026-09-25-a-rolled-back-quarantine-is-settled.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import reclaim_runtime_state
from markdown_transaction import MarkdownCoordinator

from tests.test_every_store_has_a_bound import _append


def _states(coordinator: MarkdownCoordinator) -> list[tuple[str, str | None]]:
    with sqlite3.connect(coordinator.database_path) as database:
        return database.execute('SELECT state, error_code FROM "transaction" ORDER BY operation_id').fetchall()


def _quarantine(coordinator: MarkdownCoordinator, applied: int) -> None:
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute("""UPDATE "transaction" SET state = 'quarantined', error_code = 'precondition_failed'""")
        database.execute("UPDATE operation SET applied = ?", (applied,))


def test_a_quarantine_with_nothing_applied_is_discarded(tmp_path: Path) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    _append(coordinator, "2026-01-01")
    _quarantine(coordinator, applied=0)

    settled = coordinator.settle_rolled_back_quarantines()

    assert (settled, _states(coordinator)) == (1, [("discarded", "precondition_failed")])


def test_a_quarantine_with_an_operation_still_applied_stays(tmp_path: Path) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    _append(coordinator, "2026-01-01")
    _quarantine(coordinator, applied=1)

    assert (coordinator.settle_rolled_back_quarantines(), _states(coordinator)) == (
        0, [("quarantined", "precondition_failed")],
    )


def test_empty_shards_under_ready_are_removed_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(reclaim_runtime_state, "STATE_ROOT", tmp_path)
    for stage in ("pending", "ready"):
        (tmp_path / "run/capture-intents" / stage / "0a").mkdir(parents=True)

    assert reclaim_runtime_state.remove_empty_intent_shards() == 2
