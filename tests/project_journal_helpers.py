"""Helpers shared by test_project_journal.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest
from project_journal import ProjectStore


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/projects/demo").mkdir(parents=True)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def checkpoint_event(
    occurrence_id: str = "evt-1",
    idempotency_key: str = "task:task-1:active",
    *,
    delta: dict[str, object] | None = None,
) -> dict[str, object]:
    complete_delta: dict[str, object] = {
        "goal": {"id": "goal-1", "action": "upsert", "value": "Ship Stage 2"},
        "phase": {"id": "phase-1", "action": "upsert", "value": "Implementation"},
        "current_task": {
            "id": "task-1",
            "action": "upsert",
            "value": "Build project journals",
        },
        "next_actions": [
            {"id": "next-1", "action": "upsert", "value": "Run recovery tests"}
        ],
        "decisions": [
            {
                "id": "decision-1",
                "action": "upsert",
                "value": "Use fenced Markdown transactions",
            }
        ],
        "blockers": [
            {"id": "blocker-1", "action": "upsert", "value": "None"}
        ],
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
    }
    if delta:
        complete_delta.update(delta)
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": occurrence_id,
        "idempotency_key": idempotency_key,
        "provenance": {
            "agent": "agent-a",
            "session": "session-1",
            "worktree": "D:/work/wiki",
            "branch": "feature/journal",
            "source_event": "tool-42",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": complete_delta,
        "evidence_event_ids": ["tool-41", "tool-42"],
    }


@pytest.fixture
def project_store(vault: Path, state_root: Path) -> ProjectStore:  # noqa: F811
    return ProjectStore(vault, state_root)


def state_body(state: bytes) -> list[str]:
    """The rendered sections, without the sequence line that legitimately moves."""
    return [
        line
        for line in state.decode("utf-8").splitlines()
        if not line.startswith("last_applied_sequence:")
    ]

