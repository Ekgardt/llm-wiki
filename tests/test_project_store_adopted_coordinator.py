"""ProjectStore must open the coordinator adoption made active, not the tombstone.

Reliability V3 adoption replaces `run/markdown-transactions.sqlite3` with a JSON
tombstone. A writer that constructs `MarkdownCoordinator(vault, state_root)`
itself opens that path and dies with `file is not a database`, which on the live
vault silently stopped every project checkpoint. The rule that decides which
coordinator a writer gets lives in `markdown_transaction`; every writer asks it
rather than choosing for itself.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from installed_memory_repair import repair_installed_vault  # noqa: E402
from project_journal import ProjectStore  # noqa: E402

LEGACY_COORDINATOR = "run/markdown-transactions.sqlite3"
ADOPTED_COORDINATOR = "run/markdown-transactions-v3.sqlite3"


def checkpoint_event() -> dict[str, object]:
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "evt-adopted-1",
        "idempotency_key": "task:adopted-1:active",
        "provenance": {
            "agent": "agent-a",
            "session": "session-1",
            "worktree": "D:/work/wiki",
            "branch": "feature/adoption",
            "source_event": "tool-1",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": {
            "goal": {"id": "goal-1", "action": "upsert", "value": "Survive adoption"},
            "phase": {"id": "phase-1", "action": "upsert", "value": "Implementation"},
            "current_task": {
                "id": "task-1",
                "action": "upsert",
                "value": "Checkpoint through the adopted coordinator",
            },
            "next_actions": [
                {"id": "next-1", "action": "upsert", "value": "Drain the queue"}
            ],
            "decisions": [
                {"id": "decision-1", "action": "upsert", "value": "Ask for the rule"}
            ],
            "blockers": [{"id": "blocker-1", "action": "upsert", "value": "None"}],
            "changed_files": [
                {
                    "id": "file-1",
                    "action": "upsert",
                    "value": "scripts/project_journal.py",
                }
            ],
            "commands": [
                {"id": "command-1", "action": "upsert", "value": "uv run pytest"}
            ],
            "verification": [
                {"id": "verify-1", "action": "upsert", "value": "project tests pass"}
            ],
        },
        "evidence_event_ids": ["tool-1"],
    }


@pytest.fixture
def adopted_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault that has completed the one-way Reliability V3 adoption."""
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok"
    return root, state_root


def test_adoption_leaves_the_legacy_coordinator_path_unopenable(
    adopted_vault: tuple[Path, Path],
) -> None:
    """The precondition the writer has to survive: the old path is not a database."""
    _root, state_root = adopted_vault
    legacy = state_root / LEGACY_COORDINATOR

    assert legacy.is_file()
    connection = sqlite3.connect(legacy)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        connection.close()


def test_project_store_opens_the_adopted_coordinator(
    adopted_vault: tuple[Path, Path],
) -> None:
    root, state_root = adopted_vault

    store = ProjectStore(root, state_root)

    assert store.coordinator.database_path == state_root / ADOPTED_COORDINATOR


def _checkpoint_once(adopted_vault: tuple[Path, Path]):
    root, state_root = adopted_vault
    (root / "knowledge/projects/demo").mkdir(parents=True, exist_ok=True)
    store = ProjectStore(root, state_root)
    return store.checkpoint("demo", checkpoint_event(), "agent-a")


def test_project_store_checkpoints_on_an_adopted_vault(
    adopted_vault: tuple[Path, Path],
) -> None:
    receipt = _checkpoint_once(adopted_vault)

    assert receipt.sequence == 1
    assert receipt.transaction_id


def test_the_adopted_checkpoint_writes_the_journal_and_the_state(
    adopted_vault: tuple[Path, Path],
) -> None:
    root, _state_root = adopted_vault

    _checkpoint_once(adopted_vault)

    journal = root / "knowledge/projects/demo/journal.md"
    assert b"Checkpoint through the adopted coordinator" in journal.read_bytes()
    assert (root / "knowledge/projects/demo/state.md").is_file()
