"""A checkpoint whose commit failed is retried as the same batch, even after new events arrive.

The retry planned a larger batch with a new id, and the journal got a second entry
repeating what was already written. Research:
`docs/research/2026-09-14-a-retried-checkpoint-is-the-same-batch.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from llm_wiki import event_project, integration_session_start

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_adapter  # noqa: E402
import project_checkpoint  # noqa: E402


def test_a_new_event_does_not_widen_the_batch_a_failed_commit_left(monkeypatch):
    state: dict = {}
    updates = 0
    checkpoints: list[str] = []

    def update(mutator, **kwargs):
        nonlocal updates
        updates += 1
        if updates == 4:  # claim, in-flight record, journal written, then this commit
            raise TimeoutError("commit state busy")
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner):
            checkpoints.append(event["occurrence_id"])

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    one = integration_adapter.normalize_event("codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "one"})
    two = integration_adapter.normalize_event("codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "two"})

    with pytest.raises(TimeoutError):
        project_checkpoint.observe_project_checkpoint(one)
    project_checkpoint.observe_project_checkpoint(two)

    batch = project_checkpoint.batch_occurrence_id
    assert (checkpoints, state["project_checkpoint_pending"]["demo"]) == (
        [batch([one.event_id]), batch([one.event_id]), batch([two.event_id])],
        [],
    )
