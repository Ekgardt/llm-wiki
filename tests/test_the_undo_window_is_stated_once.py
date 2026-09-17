"""The coordinator's undo window is the one the rest of the system uses.

`UNDO_RETENTION_DAYS` became two on 2026-09-02, and two literals kept thirty: an
undo was refused only after a month, and a committed transaction blocked the
deletion of `run/` for a month while the doctor stopped counting it after two days.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from markdown_transaction import (
    UNDO_RETENTION_DAYS,
    MarkdownChange,
    MarkdownCoordinator,
)


def _committed_days_ago(tmp_path: Path, days: int) -> tuple[MarkdownCoordinator, str]:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    record = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")], operation_id="aged"
    )
    coordinator.apply(record.id)
    aged = datetime.now(timezone.utc) - timedelta(days=days)
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (aged.isoformat().replace("+00:00", "Z"), record.id),
        )
    return coordinator, record.id


def test_an_undo_just_past_the_window_is_refused_as_expired(tmp_path: Path) -> None:
    coordinator, transaction_id = _committed_days_ago(tmp_path, UNDO_RETENTION_DAYS + 1)

    with pytest.raises(RuntimeError, match=f"{UNDO_RETENTION_DAYS}-day undo window"):
        coordinator.undo(transaction_id)


def test_a_transaction_past_the_window_no_longer_blocks_deletion(tmp_path: Path) -> None:
    coordinator, _transaction_id = _committed_days_ago(tmp_path, UNDO_RETENTION_DAYS + 1)

    assert coordinator.deletion_blockers() == []


def test_a_transaction_inside_the_window_still_blocks_deletion(tmp_path: Path) -> None:
    coordinator, transaction_id = _committed_days_ago(tmp_path, 0)

    blockers = coordinator.deletion_blockers()

    assert [item["transaction_id"] for item in blockers] == [transaction_id]
