"""A project checkpoint cancelled at the commit leaves the vault as it was.

Every project target moves inside the database transaction that commits the
checkpoint, so that a failure rewinds both together. The restore sat inside the
lock, and `before_commit` — where the caller's deadline or cancellation lands —
runs as the lock exits: the database rewound and `journal.md` kept its new bytes.

See `docs/research/2026-09-18-the-restore-covers-the-commit-too.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from markdown_transaction import MarkdownChange  # noqa: E402
from project_journal import ProjectStore  # noqa: E402

_JOURNAL = "knowledge/projects/demo/journal.md"
_BEFORE = b"before\n"
_AFTER = b"after\n"


def _store(tmp_path: Path) -> ProjectStore:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    (vault / _JOURNAL).write_bytes(_BEFORE)
    return ProjectStore(vault, tmp_path / "state")


def _precondition(lease) -> dict[str, object]:
    return {
        "project": "demo",
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
    }


def _event() -> dict[str, object]:
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "evt-1",
        "idempotency_key": "task:task-1:active",
        "provenance": {
            "agent": "agent-a",
            "session": "session-1",
            "worktree": "/work/wiki",
            "branch": "feature/journal",
            "source_event": "tool-42",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": _delta(),
        "evidence_event_ids": ["tool-42"],
    }


_ONE_OF = ("goal", "phase", "current_task")
_MANY_OF = (
    "next_actions",
    "decisions",
    "blockers",
    "changed_files",
    "commands",
    "verification",
)


def _entry(name: str) -> dict[str, object]:
    return {"id": f"{name}-1", "action": "upsert", "value": name}


def _delta() -> dict[str, object]:
    """Every field the checkpoint schema requires, with one entry each."""
    delta: dict[str, object] = {name: _entry(name) for name in _ONE_OF}
    delta.update({name: [_entry(name)] for name in _MANY_OF})
    return delta


def _cancelled_at_the_commit(store: ProjectStore, target: Path):
    """True only once the bytes have moved and the lock is on its way out.

    `mutation_database` is cleared in the lock's own `finally`, which runs
    before `before_commit`, so this says False everywhere inside the body and
    True exactly at the commit.
    """
    coordinator = store.coordinator

    def cancelled() -> bool:
        moved = target.read_bytes() == _AFTER
        return moved and getattr(coordinator._local, "mutation_database", None) is None

    return cancelled


def test_a_checkpoint_cancelled_at_the_commit_leaves_the_journal_as_it_was(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    target = store.coordinator.vault / _JOURNAL
    lease = store.acquire_lease("demo", "agent-a")
    precondition = _precondition(lease)
    reservation = store.coordinator.reserve_project_checkpoint(
        "demo", _event(), precondition
    )
    record = store.coordinator.prepare(
        [MarkdownChange.replace(_JOURNAL, _AFTER)],
        operation_id=reservation.operation_id,
        preconditions={"project_lease": precondition},
        project_reservation=reservation,
    )

    with pytest.raises(TimeoutError):
        store.coordinator.apply(
            record.id, cancelled=_cancelled_at_the_commit(store, target)
        )

    assert target.read_bytes() == _BEFORE
