"""A reservation on a checkpoint that another attempt committed is dropped at once.

See docs/research/2026-09-25-a-spent-checkpoint-reservation-is-dropped.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from markdown_transaction import MarkdownCoordinator

NOW = "2026-09-25T00:00:00Z"


def _attempt(database, number: int, state: str) -> None:
    database.execute(
        "INSERT INTO project_checkpoint_attempts (project, sequence, attempt_number, operation_id, "
        "parent_operation_id, lease_token, fencing_epoch, transaction_id, state, created_at) "
        "VALUES ('demo', 1, ?, ?, NULL, 'lease', 1, NULL, ?, ?)",
        (number, f"project:demo:1:{number}", state, NOW),
    )


def test_only_the_spent_reservation_goes(tmp_path: Path) -> None:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "INSERT INTO project_checkpoints (project, sequence, occurrence_id, idempotency_key, event_json, "
            "lease_token, fencing_epoch, operation_id, attempt_number, parent_operation_id, transaction_id, state) "
            "VALUES ('demo', 1, 'o1', 'k1', '{}', 'lease', 1, 'project:demo:1:2', 2, NULL, NULL, 'committed')"
        )
        _attempt(database, 1, "reserved")
        _attempt(database, 2, "committed")

    dropped = coordinator.prune_history()

    with sqlite3.connect(coordinator.database_path) as database:
        left = database.execute("SELECT attempt_number, state FROM project_checkpoint_attempts").fetchall()
    assert (dropped["attempts"], left) == (1, [(2, "committed")])
