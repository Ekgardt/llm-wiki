"""The checkpoint tables' transaction keys are indexed (audit 2026-09-27 B-1, owner approved).

docs/research/2026-09-27-the-checkpoint-keys-are-indexed.md
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import markdown_transaction

INDEXES = {"project_checkpoints_transaction", "project_checkpoint_attempts_transaction"}


def _database(tmp_path: Path) -> Path:
    (tmp_path / "run").mkdir()
    path = tmp_path / "run" / "markdown-transactions-v3.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path


def _indexes(path: Path) -> set[str]:
    with sqlite3.connect(path) as database:
        rows = database.execute("SELECT name FROM sqlite_schema WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        return {row[0] for row in rows}


def _complete(path: Path) -> bool:
    with sqlite3.connect(path) as database:
        return markdown_transaction.coordinator_v3_schema_complete(database)


def test_a_database_built_before_the_indexes_is_still_complete(tmp_path: Path) -> None:
    path = _database(tmp_path)

    assert (_indexes(path), _complete(path)) == (set(), True)


def test_the_history_prune_creates_them_and_the_database_stays_complete(tmp_path: Path) -> None:
    path = _database(tmp_path)
    coordinator = markdown_transaction.MarkdownCoordinator.from_v3_candidate(path, state_root=tmp_path)

    coordinator.prune_history()

    assert (_indexes(path), _complete(path)) == (INDEXES, True)


def test_an_unknown_index_still_makes_the_schema_incomplete(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute('CREATE INDEX stray ON "transaction"(state)')

    assert _complete(path) is False
