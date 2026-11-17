from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT

_CHECKPOINT = """
import json, sys
from pathlib import Path
from project_journal import ProjectStore
store = ProjectStore(Path(sys.argv[1]), Path(sys.argv[2]))
store.checkpoint('demo', json.loads(sys.argv[3]), 'agent-a')
"""

_EVENT = {
    "schema_version": "project-checkpoint/v1",
    "occurrence_id": "evt-crash",
    "idempotency_key": "checkpoint:crash",
    "provenance": {
        "agent": "agent-a",
        "session": "session-1",
        "worktree": "D:/work/wiki",
        "branch": "feature/journal",
        "source_event": "event-1",
    },
    "trigger": "task_completed",
    "reason": "crash recovery",
    "delta": {
        "goal": {"id": "g", "action": "upsert", "value": "Recover"},
        "phase": {"id": "p", "action": "upsert", "value": "Test"},
        "current_task": {"id": "t", "action": "upsert", "value": "Replay"},
        "next_actions": [],
        "decisions": [],
        "blockers": [],
        "changed_files": [],
        "commands": [],
        "verification": [],
    },
    "evidence_event_ids": ["event-1"],
}


class _Crashed:
    def __init__(self, vault: Path, state_root: Path) -> None:
        self.vault = vault
        self.state_root = state_root
        self.database = state_root / "run/markdown-transactions.sqlite3"

    def execute(self, statement: str, *values: object) -> None:
        with sqlite3.connect(self.database) as database:
            database.execute(statement, values)

    def column(self, statement: str) -> list[object]:
        with sqlite3.connect(self.database) as database:
            return [row[0] for row in database.execute(statement)]

    def coordinator(self) -> MarkdownCoordinator:
        return MarkdownCoordinator(self.vault, self.state_root)


@pytest.fixture
def crashed(tmp_path: Path) -> _Crashed:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    state_root = tmp_path / "state"
    scripts = Path(__file__).parents[1] / "scripts"
    environment = os.environ | {
        "LLM_WIKI_TRANSACTION_KILLPOINT": "after_plan_fsynced",
        "PYTHONPATH": str(scripts),
    }
    finished = subprocess.run(
        [sys.executable, "-c", _CHECKPOINT, str(vault), str(state_root), json.dumps(_EVENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=LONG_TIMEOUT,
    )
    assert finished.returncode == 86, finished.stderr
    return _Crashed(vault, state_root)


def _insert_unfinished_prior(crashed: _Crashed) -> None:
    crashed.execute(
        "INSERT INTO project_checkpoints (project, sequence, occurrence_id, "
        "idempotency_key, event_json, lease_token, fencing_epoch, operation_id, state) "
        "VALUES ('demo', 0, 'evt-prior', 'checkpoint:prior', '{}', 'token', 1, "
        "'prior-operation', 'reserved')"
    )


def test_a_checkpoint_behind_an_unfinished_prior_is_discarded_at_promotion(
    crashed: _Crashed,
) -> None:
    _insert_unfinished_prior(crashed)

    recovered = crashed.coordinator().recover()

    assert [(record.state, record.error_code) for record in recovered] == [("discarded", None)]
    assert not (crashed.vault / "knowledge/projects/demo/journal.md").exists()


def test_a_checkpoint_whose_attempt_was_taken_is_quarantined_with_its_reservation(
    crashed: _Crashed,
) -> None:
    crashed.execute("UPDATE project_checkpoint_attempts SET state = 'committed'")

    recovered = crashed.coordinator().recover()

    assert [(record.state, record.error_code) for record in recovered] == [
        ("quarantined", "precondition_failed")
    ]
    assert crashed.column("SELECT state FROM project_checkpoints WHERE sequence = 1") == [
        "quarantined"
    ]
    assert crashed.column("SELECT state FROM project_checkpoint_attempts") == ["quarantined"]


def test_a_promoted_checkpoint_waits_for_a_prior_that_appeared_after_promotion(
    crashed: _Crashed,
) -> None:
    coordinator = crashed.coordinator()
    transaction_id = str(crashed.column('SELECT id FROM "transaction"')[0])
    assert coordinator._promote_preparing(coordinator._record(transaction_id)) == "promoted"
    _insert_unfinished_prior(crashed)

    recovered = coordinator.recover()

    assert [(record.state, record.error_code) for record in recovered] == [
        ("prepared", "pending_prior")
    ]
    assert not (crashed.vault / "knowledge/projects/demo/journal.md").exists()


def test_a_checkpoint_whose_reservation_was_taken_is_quarantined(
    crashed: _Crashed,
) -> None:
    crashed.execute("UPDATE project_checkpoints SET state = 'committed' WHERE sequence = 1")

    recovered = crashed.coordinator().recover()

    assert [(record.state, record.error_code) for record in recovered] == [
        ("quarantined", "precondition_failed")
    ]
    assert not (crashed.vault / "knowledge/projects/demo/journal.md").exists()
