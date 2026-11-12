"""Helpers shared by test_append_race_lineage.py and the test files importing them."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import markdown_transaction
import pytest
from llm_wiki import doctor_transactions
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT

_APPEND_BUDGET_SECONDS = LONG_TIMEOUT


_DAILY = "knowledge/daily/2026-08-25.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative in ("knowledge/daily", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    (root / _DAILY).write_bytes(b"# 2026-08-25\n")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _append(coordinator: MarkdownCoordinator, operation_id: str, block: bytes):
    return markdown_transaction.append_until_committed(
        coordinator,
        operation_id,
        _DAILY,
        block,
        deadline=time.monotonic() + _APPEND_BUDGET_SECONDS,
        cancelled=None,
    )


def _unresolved(coordinator: MarkdownCoordinator) -> int:
    with sqlite3.connect(coordinator.database_path) as database:
        columns = {
            row[1] for row in database.execute('PRAGMA table_info("transaction")')
        }
        return doctor_transactions.unresolved_quarantine(database, columns)
