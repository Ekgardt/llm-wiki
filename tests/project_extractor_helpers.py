"""Helpers shared by test_project_extractor.py and the test files importing them."""
from __future__ import annotations


def _event() -> dict[str, object]:
    close = {"id": "none", "action": "close", "value": ""}
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "checkpoint-1",
        "idempotency_key": "task:one",
        "project": "demo",
        "sequence": 1,
        "provenance": {
            "agent": "agent-a",
            "session": "session-7",
            "worktree": "D:/work/demo",
            "branch": "feature/demo",
            "source_event": "event-9",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": {
            "goal": close,
            "phase": close,
            "current_task": close,
            "next_actions": [],
            "decisions": [{"id": "decision-1", "action": "upsert", "value": "Keep bytes immutable"}],
            "blockers": [{"id": "blocker-1", "action": "upsert", "value": "Waiting for Task19"}],
            "changed_files": [{"id": "file-1", "action": "upsert", "value": "scripts/project_journal.py"}],
            "commands": [],
            "verification": [],
        },
        "evidence_event_ids": ["event-8", "event-9"],
        "last_applied_sequence": 1,
    }
