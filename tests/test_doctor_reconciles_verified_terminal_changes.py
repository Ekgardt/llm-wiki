"""Concurrent terminal commits require fresh proof, not a false unreadable verdict."""
import sqlite3
import time
from datetime import datetime, timezone

import doctor
import flush_memory
import memory_queue
import pytest
from markdown_transaction import MarkdownChange, TransactionFailure

from tests.slow_machine import LONG_TIMEOUT
from tests.test_capture_terminal import (
    _FakeNoContentProvider,
    _NoContentProcessor,
    _ready_intent_binding,
)
from tests.test_doctor_releases_snapshot_before_artifact_work import _environment


def _check(root, state):
    return doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic() + LONG_TIMEOUT, vault_root=root)


def test_committed_adopted_capture_is_reconciled_after_filesystem_inspection(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    monkeypatch.setattr(flush_memory, "ROOT", root)
    processor = _NoContentProcessor(queue, coordinator, _FakeNoContentProvider())
    original = doctor._checked_artifacts
    completed = []

    def inspect(*args):
        if not completed:
            completed.append(flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert queue.get(binding.task_id).state == "succeeded"
    assert completed == [f"run/queue-results/capture-{binding.intent_id}.json"]
    assert result["details"]["states"]["committed"] > 0
    assert result["details"]["read_error"] is False
    assert result["status"] == "ok"
    assert "transaction_undo_retained" in result["details"]["deletion_codes"]
    registry.release(lease)


def test_terminal_delta_rechecks_ordinary_committed_operation(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    original = doctor._checked_artifacts
    committed = []

    def inspect(*args):
        if not committed:
            prepared = coordinator.prepare([MarkdownChange.create("knowledge/notes/new.md", b"# New\n")], operation_id="ordinary-during-inspection")
            committed.append(coordinator.apply(prepared.id))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["details"]["read_error"] is False
    assert result["details"]["states"]["committed"] == 1
    assert result["status"] == "ok"
    assert "transaction_undo_retained" in result["details"]["deletion_codes"]
    registry.release(lease)


def _commit(coordinator, slug):
    prepared = coordinator.prepare([MarkdownChange.create(f"knowledge/notes/{slug}.md", b"# Page\n")], operation_id=slug)
    return coordinator.apply(prepared.id)


def test_unchanged_row_contribution_is_reused_but_artifacts_are_rescanned(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    existing = _commit(coordinator, "existing")
    original = doctor._checked_artifacts
    contribution = doctor._transaction_row_contribution
    rows, inventories, commits = [], [], []

    def inspect(*args):
        inventories.append(1)
        if not commits:
            commits.append(_commit(coordinator, "later"))
        return original(*args)

    def compute(row, *args):
        rows.append(row["id"])
        return contribution(row, *args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    monkeypatch.setattr(doctor, "_transaction_row_contribution", compute)
    result = _check(root, state)
    assert result["details"]["read_error"] is False
    assert result["details"]["states"]["committed"] == 2
    assert inventories == [1, 1]
    assert rows.count(existing.id) == 1
    assert rows.count(commits[0].id) == 1
    registry.release(lease)


def _tamper_operation(coordinator, identifier, mutation):
    sql = {
        "bad-hash": "UPDATE operation SET after_hash='invalid' WHERE transaction_id=?",
        "position-gap": "UPDATE operation SET position=5 WHERE transaction_id=?",
        "orphan": "UPDATE operation SET transaction_id='missing' WHERE transaction_id=?",
        "valid-hash": "UPDATE operation SET after_hash='" + "a" * 64 + "' WHERE transaction_id=?",
        "valid-path": "UPDATE operation SET path='knowledge/notes/other.md' WHERE transaction_id=?",
        "valid-applied": "UPDATE operation SET applied=0 WHERE transaction_id=?",
    }
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(sql[mutation], (identifier,))


@pytest.mark.parametrize("mutation", ["bad-hash", "position-gap", "orphan", "valid-hash", "valid-path", "valid-applied"])
def test_terminal_operation_delta_is_never_ignored(tmp_path, monkeypatch, mutation):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    committed = _commit(coordinator, "original")
    original = doctor._checked_artifacts
    changed = []

    def inspect(*args):
        if not changed:
            changed.append(1)
            _tamper_operation(coordinator, committed.id, mutation)
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["status"] == "error"
    assert result["details"]["read_error"] or result["details"]["state_invalid"]
    assert result["details"]["deletion_codes"]
    registry.release(lease)


@pytest.mark.parametrize("name", ["UNKNOWN", "unknown-valid-name"])
def test_unknown_artifact_during_terminal_delta_is_not_accepted(tmp_path, monkeypatch, name):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    original = doctor._checked_artifacts
    committed = []

    def inspect(*args):
        if not committed:
            committed.append(_commit(coordinator, "later"))
            (state / "run/transactions" / name).mkdir()
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["details"]["read_error"] is True
    assert "transaction_artifact_state_unknown" in result["details"]["deletion_codes"]
    registry.release(lease)


def _refused(coordinator):
    prepared = coordinator.prepare([MarkdownChange.create("knowledge/notes/refused.md", b"# Refused\n")], operation_id="compile:refused-delta", preconditions={"knowledge/notes/missing.md": "0" * 64})
    with pytest.raises(TransactionFailure):
        coordinator.apply(prepared.id)
    return prepared


def test_terminal_change_in_late_quarantine_proof_is_reconciled(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    _refused(coordinator)
    commits, proofs = [], []

    def inspect(self, database, identifier, committed_creates):
        proofs.append(identifier)
        if not commits:
            commits.append(_commit(coordinator, "late"))
        return False

    monkeypatch.setattr(doctor._CompiledDaySupersession, "resolves", inspect)
    result = _check(root, state)
    assert result["details"]["read_error"] is False
    assert result["details"]["quarantined_unresolved"] == 1
    assert result["details"]["states"]["committed"] == 1
    assert result["status"] == "error"
    assert len(proofs) == 2
    registry.release(lease)


def test_original_caller_deadline_stops_terminal_delta_reconciliation(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    original_inventory = doctor._checked_artifacts
    original_finish = doctor._finish_reconciled_snapshot
    original_expired = doctor._deadline_reached
    committed, expired = [], []

    def inspect(*args):
        if not committed:
            committed.append(_commit(coordinator, "later"))
        return original_inventory(*args)

    def finish(*args):
        result = original_finish(*args)
        expired.append(1)
        return result

    def deadline_reached(deadline):
        return bool(expired) or original_expired(deadline)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    monkeypatch.setattr(doctor, "_finish_reconciled_snapshot", finish)
    monkeypatch.setattr(doctor, "_deadline_reached", deadline_reached)
    result = _check(root, state)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    assert len(committed) == 1
    registry.release(lease)


@pytest.mark.parametrize("state_value", ["prepared", "applying", "aborting", "unrecognized"])
def test_changed_nonterminal_or_unknown_state_stays_unreadable(tmp_path, monkeypatch, state_value):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    committed = _commit(coordinator, "original")
    original = doctor._checked_artifacts
    changed = []

    def inspect(*args):
        if not changed:
            changed.append(1)
            with sqlite3.connect(coordinator.database_path) as database:
                database.execute("PRAGMA ignore_check_constraints=ON")
                database.execute('UPDATE "transaction" SET state=? WHERE id=?', (state_value, committed.id))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    assert "transaction_state_unreadable" in result["details"]["deletion_codes"]
    registry.release(lease)


def test_disappearing_transaction_is_not_silently_removed_from_verdict(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    committed = _commit(coordinator, "original")
    original = doctor._checked_artifacts
    changed = []

    def inspect(*args):
        if not changed:
            changed.append(1)
            with sqlite3.connect(coordinator.database_path) as database:
                database.execute('DELETE FROM "transaction" WHERE id=?', (committed.id,))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["details"]["read_error"] is True
    assert result["status"] == "error"
    registry.release(lease)


@pytest.mark.parametrize("table", ["transaction", "operation"])
def test_changed_sql_columns_refuse_reconciliation(tmp_path, monkeypatch, table):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    _commit(coordinator, "original")
    original = doctor._checked_artifacts
    changed = []

    def inspect(*args):
        if not changed:
            changed.append(1)
            with sqlite3.connect(coordinator.database_path) as database:
                database.execute(f'ALTER TABLE "{table}" ADD COLUMN unexpected TEXT')
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["details"]["read_error"] is True
    assert result["status"] == "error"
    registry.release(lease)


@pytest.mark.parametrize("field,value", [
    ("request_hash", "b" * 64),
    ("operation_id", "changed-identity"),
    ("plan_hash", "c" * 64),
    ("preconditions_json", '{"changed":"absent"}'),
    ("created_at", "2020-01-01T00:00:00.000000+00:00"),
    ("updated_at", "2020-01-01T00:00:00.000000+00:00"),
])
def test_previous_terminal_transaction_fields_are_immutable(tmp_path, monkeypatch, field, value):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    committed = _commit(coordinator, "original")
    original = doctor._checked_artifacts
    changed = []

    def inspect(*args):
        if not changed:
            changed.append(1)
            with sqlite3.connect(coordinator.database_path) as database:
                database.execute(f'UPDATE "transaction" SET {field}=? WHERE id=?', (value, committed.id))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    registry.release(lease)


def test_previously_prepared_transaction_can_become_verified_terminal(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    prepared = coordinator.prepare([MarkdownChange.create("knowledge/notes/pending.md", b"# Pending\n")], operation_id="pending-to-terminal")
    original = doctor._checked_artifacts
    committed = []

    def inspect(*args):
        if not committed:
            committed.append(coordinator.apply(prepared.id))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = _check(root, state)
    assert result["status"] == "ok"
    assert result["details"]["read_error"] is False
    assert result["details"]["states"]["prepared"] == 0
    assert result["details"]["states"]["committed"] == 1
    assert "transaction_nonterminal" not in result["details"]["deletion_codes"]
    assert "transaction_undo_retained" in result["details"]["deletion_codes"]
    registry.release(lease)
