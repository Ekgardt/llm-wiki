from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from markdown_transaction import MarkdownChange
from project_journal import (
    JOURNAL_HEADER,
    ProjectFenceError,
    ProjectLeaseBusy,
    ProjectStore,
)
from reliable_memory import SchemaValidationError, canonical_json_bytes, validate_schema


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/projects/demo").mkdir(parents=True)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def project_store(vault: Path, state_root: Path) -> ProjectStore:
    return ProjectStore(vault, state_root)


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


def journal_records(store: ProjectStore, slug: str = "demo") -> list[dict[str, object]]:
    text = store.read_journal(slug)
    return [json.loads(line) for line in text.removeprefix(JOURNAL_HEADER).splitlines()]


def test_lease_uses_random_token_monotonic_epoch_and_default_timing(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)

    first = store.acquire_lease("demo", "agent-a")
    assert first.expires_at == now + timedelta(seconds=30)
    assert first.heartbeat_due_at == now + timedelta(seconds=10)

    now += timedelta(seconds=31)
    second = store.acquire_lease("demo", "agent-b")
    assert second.token != first.token
    assert second.epoch == first.epoch + 1


def test_coordinator_reserves_idempotency_sequence_and_preparation_binding(
    vault: Path, state_root: Path
):
    store = ProjectStore(vault, state_root)
    lease = store.acquire_lease("demo", "agent-a")
    precondition = {
        "project": "demo",
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
    }

    reservation = store.coordinator.reserve_project_checkpoint(
        "demo", checkpoint_event(), precondition
    )
    duplicate = store.coordinator.reserve_project_checkpoint(
        "demo", checkpoint_event("evt-retry"), precondition
    )
    transaction = store.coordinator.prepare(
        [
            MarkdownChange.create(
                "knowledge/projects/demo/journal.md",
                b"reserved-event\n",
            )
        ],
        operation_id=reservation.operation_id,
        preconditions={"project_lease": precondition},
        project_reservation=reservation,
    )

    assert reservation.sequence == duplicate.sequence == 1
    assert duplicate.duplicate is True
    with sqlite3.connect(store.coordinator.database_path) as database:
        row = database.execute(
            "SELECT sequence, operation_id, transaction_id, state "
            "FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()
    assert row == (1, reservation.operation_id, transaction.id, "prepared")


def test_active_lease_is_exclusive_and_heartbeat_extends_it(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    lease = store.acquire_lease("demo", "agent-a")

    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-b")

    now += timedelta(seconds=10)
    renewed = store.heartbeat(lease)
    assert renewed.expires_at == now + timedelta(seconds=30)
    assert renewed.heartbeat_due_at == now + timedelta(seconds=10)


def test_same_owner_cannot_alias_active_lease_and_exact_token_can_renew(
    vault: Path, state_root: Path
):
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    store = ProjectStore(vault, state_root, clock=lambda: now)
    lease = store.acquire_lease("demo", "agent-a")

    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-a")
    with pytest.raises(ProjectLeaseBusy):
        store.acquire_lease("demo", "agent-a", token="wrong-token")

    now += timedelta(seconds=5)
    renewed = store.acquire_lease("demo", "agent-a", token=lease.token)

    assert renewed.token == lease.token
    assert renewed.epoch == lease.epoch
    assert renewed.expires_at == now + timedelta(seconds=30)


@pytest.mark.parametrize("slug", ["../demo", "Demo", "demo/name", "", "demo_1"])
def test_slug_safety_is_preserved(project_store: ProjectStore, slug: str):
    with pytest.raises(ValueError):
        project_store.acquire_lease(slug, "agent-a")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.pop("provenance"),
        lambda event: event.update(unexpected=True),
        lambda event: event["delta"]["goal"].update(action="invalid"),
    ],
)
def test_invalid_event_does_not_reserve_sequence_or_idempotency(
    project_store: ProjectStore, mutate
):
    invalid = checkpoint_event()
    mutate(invalid)

    with pytest.raises(SchemaValidationError):
        project_store.checkpoint("demo", invalid, "agent-a")

    with sqlite3.connect(project_store.coordinator.database_path) as database:
        assert database.execute("SELECT * FROM project_checkpoints").fetchall() == []
    receipt = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    assert receipt.sequence == 1


def test_event_is_normalized_before_reservation(project_store: ProjectStore):
    event = checkpoint_event("e\u0301vt", "ke\u0301y")
    event["delta"]["goal"]["value"] = "Cafe\u0301"

    receipt = project_store.checkpoint("demo", event, "agent-a")
    record = journal_records(project_store)[0]

    assert receipt.occurrence_id == "\u00e9vt"
    assert receipt.idempotency_key == "k\u00e9y"
    assert record["delta"]["goal"]["value"] == "Caf\u00e9"


def test_checkpoint_is_append_only_idempotent_and_projects_state(
    project_store: ProjectStore,
):
    first = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    before = project_store.read_journal("demo")
    duplicate = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    second = project_store.checkpoint(
        "demo",
        checkpoint_event("evt-2", "blocker:blocker-1:closed", delta={
            "blockers": [
                {"id": "blocker-1", "action": "close", "value": "resolved"}
            ],
            "current_task": {
                "id": "task-1",
                "action": "upsert",
                "value": "Review journal recovery",
            },
        }),
        "agent-a",
    )

    assert first.sequence == duplicate.sequence == 1
    assert duplicate.duplicate is True
    assert second.sequence == 2
    assert project_store.read_journal("demo").startswith(before)
    records = journal_records(project_store)
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["last_applied_sequence"] == 0
    assert records[1]["last_applied_sequence"] == 1
    validate_schema(records[0], Path("scripts/schemas/project-checkpoint-v1.json"))

    state = (project_store.vault / "knowledge/projects/demo/state.md").read_text(
        encoding="utf-8"
    )
    assert "generated: true" in state
    assert "last_applied_sequence: 2" in state
    assert "Review journal recovery" in state
    assert "blocker-1" not in state
    assert "Use fenced Markdown transactions" in state


def test_idempotency_key_deduplicates_a_new_occurrence(project_store: ProjectStore):
    first = project_store.checkpoint("demo", checkpoint_event(), "agent-a")
    duplicate = project_store.checkpoint(
        "demo", checkpoint_event("evt-retried"), "agent-a"
    )

    assert (first.sequence, duplicate.sequence, duplicate.duplicate) == (1, 1, True)
    assert len(journal_records(project_store)) == 1


def test_idempotency_key_rejects_changed_payload(project_store: ProjectStore):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")

    with pytest.raises(ValueError, match="idempotency_key"):
        project_store.checkpoint(
            "demo",
            checkpoint_event(
                "evt-retried",
                delta={
                    "current_task": {
                        "id": "task-1",
                        "action": "upsert",
                        "value": "A different operation",
                    }
                },
            ),
            "agent-a",
        )


def test_conflicting_occurrence_id_is_rejected(project_store: ProjectStore):
    project_store.checkpoint("demo", checkpoint_event(), "agent-a")

    with pytest.raises(ValueError, match="occurrence_id"):
        project_store.checkpoint(
            "demo",
            checkpoint_event("evt-1", "different-key"),
            "agent-a",
        )


def test_render_state_is_deterministic_and_bounded(project_store: ProjectStore):
    events = []
    for index in range(20):
        event = checkpoint_event(f"evt-{index}", f"decision-{index}")
        event.update(project="demo", sequence=index + 1, last_applied_sequence=index)
        event["delta"]["decisions"] = [
            {
                "id": f"decision-{index}",
                "action": "upsert",
                "value": "x" * 1000,
            }
        ]
        for section in (
            "next_actions",
            "blockers",
            "changed_files",
            "commands",
            "verification",
        ):
            event["delta"][section] = [
                {
                    "id": f"{section}-{index}",
                    "action": "upsert",
                    "value": "x" * 1000,
                }
            ]
        events.append(event)

    first = project_store.render_state(events)
    second = project_store.render_state(list(events))

    assert first == second
    assert len(first) <= 12_000
    assert first.count(b"- `decision-") <= 5
    assert b"last_applied_sequence: 20" in first


def test_final_apply_rechecks_lease_and_rejects_stale_epoch(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    real_apply = store.coordinator.apply

    def steal_then_apply(transaction_id: str):
        with sqlite3.connect(store.coordinator.database_path) as database:
            database.execute(
                "UPDATE project_leases SET lease_token = 'new-token', "
                "fencing_epoch = fencing_epoch + 1 WHERE project = 'demo'"
            )
            database.commit()
        return real_apply(transaction_id)

    monkeypatch.setattr(store.coordinator, "apply", steal_then_apply)

    with pytest.raises(ProjectFenceError):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert not (vault / "knowledge/projects/demo/state.md").exists()


def test_valid_checkpoint_after_fenced_reservation_records_last_applied_sequence(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    real_apply = store.coordinator.apply

    def steal_then_apply(transaction_id: str):
        with sqlite3.connect(store.coordinator.database_path) as database:
            database.execute(
                "UPDATE project_leases SET lease_token = 'new-token', "
                "fencing_epoch = fencing_epoch + 1 WHERE project = 'demo'"
            )
            database.commit()
        return real_apply(transaction_id)

    monkeypatch.setattr(store.coordinator, "apply", steal_then_apply)
    with pytest.raises(ProjectFenceError):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    monkeypatch.setattr(store.coordinator, "apply", real_apply)
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    receipt = store.checkpoint(
        "demo", checkpoint_event("evt-2", "event-after-fence"), "agent-a"
    )
    event = journal_records(store)[0]

    assert receipt.sequence == 2
    assert event["sequence"] == 2
    assert event["last_applied_sequence"] == 0


def test_recover_replays_prepared_checkpoint(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)
    transaction_id = ""

    def crash_before_apply(candidate: str):
        nonlocal transaction_id
        transaction_id = candidate
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")

    recovered = ProjectStore(vault, state_root).recover("demo")

    assert transaction_id
    assert [(receipt.sequence, receipt.duplicate) for receipt in recovered] == [(1, False)]
    assert len(journal_records(ProjectStore(vault, state_root))) == 1
    assert (vault / "knowledge/projects/demo/state.md").exists()


def test_new_epoch_wins_before_recovery_and_old_checkpoint_touches_nothing(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()

    assert ProjectStore(vault, state_root).recover("demo") == []
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert not (vault / "knowledge/projects/demo/state.md").exists()
    with sqlite3.connect(store.coordinator.database_path) as database:
        state = database.execute(
            "SELECT state FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()[0]
    assert state == "quarantined"


def test_recover_replays_reservation_left_before_prepare(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(vault, state_root)

    def crash_before_prepare(*args, **kwargs):
        raise RuntimeError("simulated pre-prepare crash")

    monkeypatch.setattr(store.coordinator, "prepare", crash_before_prepare)
    with pytest.raises(RuntimeError, match="pre-prepare"):
        store.checkpoint("demo", checkpoint_event(), "agent-a")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()

    recovered = ProjectStore(vault, state_root).recover("demo")

    assert [receipt.sequence for receipt in recovered] == [1]
    assert len(journal_records(ProjectStore(vault, state_root))) == 1


def test_simultaneous_projectors_append_once_per_event(vault: Path, state_root: Path):
    barrier = threading.Barrier(2)

    def write(index: int):
        store = ProjectStore(vault, state_root)
        barrier.wait()
        for _ in range(100):
            try:
                return store.checkpoint(
                    "demo",
                    checkpoint_event(f"evt-{index}", f"event-{index}"),
                    f"agent-{index}",
                )
            except ProjectLeaseBusy:
                continue
        raise AssertionError("project lease never became available")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(write, range(2)))

    assert sorted(receipt.sequence for receipt in receipts) == [1, 2]
    records = journal_records(ProjectStore(vault, state_root))
    assert {record["occurrence_id"] for record in records} == {"evt-0", "evt-1"}
    assert [record["sequence"] for record in records] == [1, 2]
    assert all(
        canonical_json_bytes(record).decode()
        in ProjectStore(vault, state_root).read_journal("demo")
        for record in records
    )


def test_same_owner_simultaneous_projectors_retry_without_sharing_lease(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    first_store = ProjectStore(vault, state_root)
    second_store = ProjectStore(vault, state_root)
    first_reserved = threading.Event()
    release_first = threading.Event()
    project = first_store._project_reserved

    def pause_first(reservation, lease):
        first_reserved.set()
        assert release_first.wait(5)
        return project(reservation, lease)

    monkeypatch.setattr(first_store, "_project_reserved", pause_first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            first_store.checkpoint,
            "demo",
            checkpoint_event("evt-first", "same-owner:first"),
            "agent-a",
        )
        assert first_reserved.wait(5)
        with pytest.raises(ProjectLeaseBusy):
            second_store.checkpoint(
                "demo",
                checkpoint_event("evt-second", "same-owner:second"),
                "agent-a",
            )
        release_first.set()
        first_receipt = first.result(timeout=5)

    second_receipt = second_store.checkpoint(
        "demo",
        checkpoint_event("evt-second", "same-owner:second"),
        "agent-a",
    )

    assert [first_receipt.sequence, second_receipt.sequence] == [1, 2]
    assert [record["occurrence_id"] for record in journal_records(second_store)] == [
        "evt-first",
        "evt-second",
    ]
