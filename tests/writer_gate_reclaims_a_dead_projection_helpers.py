"""Helpers shared by test_writer_gate_reclaims_a_dead_projection.py and the test files importing them."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import markdown_transaction

PROJECT_SCOPE = "project:wedged"


def _candidate(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _coordinator(state_root: Path):
    markdown_transaction.initialize_coordinator_v3_candidate(
        _candidate(state_root), source_v2=None
    )
    return markdown_transaction.MarkdownCoordinator.from_v3_candidate(
        _candidate(state_root), state_root=state_root
    )


def _gate_rows(database_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        return int(
            database.execute("SELECT COUNT(*) FROM writer_owners").fetchone()[0]
        )
