from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import operational_ownership
import pytest
from llm_wiki import markdown_prepare
from llm_wiki.markdown_records import (
    OperationBoundElsewhereError,
    ProjectCheckpointReservation,
    TransactionFailure,
)
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_NOTE = "knowledge/notes/page.md"


class _WindowsNamedOs:
    name = "nt"

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


def _create() -> list[MarkdownChange]:
    return [MarkdownChange.create(_NOTE, b"page\n")]


def _reservation(operation_id: str) -> ProjectCheckpointReservation:
    return ProjectCheckpointReservation(
        "demo", 1, "evt-1", "checkpoint:1", "{}", operation_id, 1, "reserved"
    )


def _lease() -> dict[str, object]:
    return {
        "project": "demo",
        "lease_token": "token",
        "fencing_epoch": 1,
        "expires_at": "2099-01-01T00:00:00Z",
    }


def _children(coordinator: MarkdownCoordinator) -> list[Path]:
    return sorted(coordinator.transaction_root.glob("*"))


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"operation_id": ""}, ValueError, "operation_id must be a non-empty string"),
        ({"changes": []}, ValueError, "a transaction requires at least one change"),
        ({"content_guard": "human"}, ValueError, "content_guard must be 'model_output' or None"),
        (
            {"project_reservation": "reservation"},
            TypeError,
            "project_reservation must be a ProjectCheckpointReservation",
        ),
        (
            {"project_reservation": _reservation("another-operation")},
            ValueError,
            "project reservation operation_id does not match",
        ),
        (
            {"project_reservation": _reservation("refused")},
            ValueError,
            "project reservation requires a project lease precondition",
        ),
        ({"validators": ["not callable"]}, TypeError, "validators must be callable"),
        (
            {"validators": [lambda plan: False]},
            ValueError,
            "transaction validator rejected the plan",
        ),
    ],
)
def test_a_request_that_cannot_be_bound_is_refused_and_leaves_no_page(
    coordinator: MarkdownCoordinator,
    vault: Path,
    arguments: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    request: dict[str, object] = {"changes": _create(), "operation_id": "refused"}
    request.update(arguments)

    with pytest.raises(error, match=message):
        coordinator.prepare(**cast(dict[str, Any], request))

    assert not (vault / _NOTE).exists()


def test_a_validator_sees_the_plan_and_accepts_it(coordinator: MarkdownCoordinator) -> None:
    seen: list[object] = []

    def accept(plan: Mapping[str, object]) -> bool:
        seen.append(plan["transaction_id"])
        return True

    record = coordinator.prepare(_create(), operation_id="validated", validators=[accept])

    assert (seen, record.state) == ([record.id], "prepared")


def test_a_process_without_a_start_identity_leaves_no_owner_record(
    coordinator: MarkdownCoordinator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unnamed() -> object:
        raise operational_ownership.OperationalOwnershipError("no process start")

    monkeypatch.setattr(operational_ownership, "current_process_identity", unnamed)
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()

    coordinator._record_preparer(artifact_root)

    assert markdown_prepare._preparer_record_bytes() is None
    assert list(artifact_root.iterdir()) == []


def test_stage_directories_are_not_hardened_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o755)
    stage.chmod(0o755)
    before = stat.S_IMODE(stage.stat().st_mode)
    monkeypatch.setattr(markdown_prepare, "os", _WindowsNamedOs())

    markdown_prepare._harden_stage_roots(stage)

    assert stat.S_IMODE(stage.stat().st_mode) == before


def test_stage_roots_that_cannot_be_made_are_removed_whole(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*directories: Path) -> None:
        raise PermissionError(13, "cannot harden", str(directories[0]))

    monkeypatch.setattr(markdown_prepare, "_harden_stage_roots", refuse)

    with pytest.raises(PermissionError, match="cannot harden"):
        coordinator.prepare(_create(), operation_id="unhardened")

    assert _children(coordinator) == []
    assert not (vault / _NOTE).exists()


def test_operations_that_cannot_be_persisted_are_named(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def collide(*arguments: object) -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(coordinator, "_persist_prepared_row", collide)

    with pytest.raises(RuntimeError, match="transaction operations could not be persisted"):
        coordinator.prepare(_create(), operation_id="unpersisted")

    with sqlite3.connect(coordinator.database_path) as database:
        rows = database.execute('SELECT count(*) FROM "transaction"').fetchone()
    assert (rows[0], _children(coordinator)) == (0, [])


def _insert_racing_row(
    coordinator: MarkdownCoordinator,
    transaction_id: str,
    operation_id: str,
    request_hash: str,
) -> object:
    artifact_root = coordinator.transaction_root / "racing"
    artifact_root.mkdir(parents=True)
    existing = coordinator._insert_preparing_row(
        transaction_id,
        operation_id,
        request_hash,
        {},
        "2026-11-17T00:00:00Z",
        None,
        artifact_root,
        float("inf"),
        None,
    )
    assert not artifact_root.exists()
    return existing


def _request_hash(coordinator: MarkdownCoordinator) -> str:
    return coordinator._request_hash(coordinator._normalized_changes(_create()), {})


def test_a_racing_preparer_of_the_same_request_returns_the_winning_record(
    coordinator: MarkdownCoordinator,
) -> None:
    winner = coordinator.prepare(_create(), operation_id="raced")

    existing = _insert_racing_row(
        coordinator, uuid.uuid4().hex, "raced", _request_hash(coordinator)
    )

    assert existing == winner


def test_a_racing_preparer_of_another_request_is_bound_elsewhere(
    coordinator: MarkdownCoordinator,
) -> None:
    coordinator.prepare(_create(), operation_id="raced")

    with pytest.raises(OperationBoundElsewhereError, match="already bound"):
        _insert_racing_row(coordinator, uuid.uuid4().hex, "raced", "0" * 64)


def test_a_colliding_transaction_id_without_a_bound_operation_is_refused(
    coordinator: MarkdownCoordinator,
) -> None:
    winner = coordinator.prepare(_create(), operation_id="first")

    with pytest.raises(OperationBoundElsewhereError, match="already bound"):
        _insert_racing_row(coordinator, winner.id, "second", "0" * 64)


def test_a_new_transaction_for_a_bound_operation_returns_the_bound_record(
    coordinator: MarkdownCoordinator,
) -> None:
    winner = coordinator.prepare(_create(), operation_id="bound")

    existing = coordinator._prepare_new_transaction(
        coordinator._normalized_changes(_create()),
        {},
        _request_hash(coordinator),
        operation_id="bound",
        validators=(),
        content_guard=None,
        project_reservation=None,
        parent_transaction_id=None,
        deadline=float("inf"),
        cancelled=None,
    )

    assert existing == winner
    assert len(_children(coordinator)) == 1


def test_a_lease_refresh_of_an_unknown_transaction_is_refused(
    coordinator: MarkdownCoordinator,
) -> None:
    with pytest.raises(TransactionFailure, match="requires a prepared transaction") as raised:
        coordinator.refresh_project_lease_precondition("0" * 32, _lease())

    assert (raised.value.code, raised.value.state) == ("precondition_failed", "quarantined")
