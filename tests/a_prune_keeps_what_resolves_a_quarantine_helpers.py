"""Helpers shared by test_a_prune_keeps_what_resolves_a_quarantine.py and the test files importing them."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import markdown_transaction
from llm_wiki import doctor_transactions
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import SHORT_TIMEOUT


def _append(coordinator: MarkdownCoordinator, operation_id: str, day: str):
    return markdown_transaction.append_until_committed(
        coordinator, operation_id, f"knowledge/daily/{day}.md", f"- {operation_id}\n".encode(),
        deadline=time.monotonic() + SHORT_TIMEOUT, cancelled=None,
    )


def _sql(coordinator: MarkdownCoordinator, statement: str, arguments: tuple = ()) -> None:
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(statement, arguments)


def _quarantine(coordinator: MarkdownCoordinator, record) -> None:
    _sql(coordinator, "UPDATE \"transaction\" SET state = 'quarantined' WHERE id = ?", (record.id,))


def _unresolved(coordinator: MarkdownCoordinator) -> int:
    with sqlite3.connect(coordinator.database_path) as database:
        columns = {row[1] for row in database.execute('PRAGMA table_info("transaction")')}
        return doctor_transactions.unresolved_quarantine(database, columns)


def _vault(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")
