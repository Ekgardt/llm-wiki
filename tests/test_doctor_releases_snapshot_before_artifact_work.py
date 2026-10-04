"""Doctor filesystem inspection must not lock the adopted owner registry."""
import sqlite3
import time
from datetime import datetime, timezone

import doctor
import pytest
from markdown_transaction import MarkdownChange, active_markdown_coordinator

from tests.adopted_vault import adopt


def _environment(tmp_path):
    root, state = adopt(tmp_path)
    (root / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    coordinator = active_markdown_coordinator(root, state)
    registry = coordinator._ownership_registry()
    lease = registry.acquire("doctor", scope="global", actor_id="snapshot-test", token="snapshot-token")
    return root, state, coordinator, registry, lease


def test_default_busy_owner_heartbeat_and_release_during_artifact_work(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)
    failures = []
    original = doctor._checked_artifacts

    def inspect(*args):
        try:
            registry.heartbeat(lease)
            registry.release(lease)
        except sqlite3.Error as error:
            failures.append(str(error))
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert failures == []
    assert result["status"] == "ok"
    assert result["details"]["live_maintenance_owners"] == 0


def test_changed_transaction_after_snapshot_is_unreadable_not_healthy(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    original = doctor._checked_artifacts

    def inspect(*args):
        coordinator.prepare([MarkdownChange.create("knowledge/notes/new.md", b"# New\n")], operation_id="new-during-inspection")
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    assert "transaction_state_corrupt" not in result["details"]["deletion_codes"]
    registry.release(lease)


def test_live_owner_is_still_reported_after_unlocked_inspection(tmp_path):
    root, state, _, registry, lease = _environment(tmp_path)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert result["details"]["live_maintenance_owners"] == 1
    assert "maintenance_owner_live" in result["details"]["deletion_codes"]
    registry.release(lease)


def test_deadline_during_artifact_work_is_visible_and_owner_can_release(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)

    def inspect(*args):
        raise TimeoutError("controlled artifact deadline")

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    registry.release(lease)


def test_incomplete_artifact_inventory_never_permits_deletion(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)
    def incomplete(*args):
        return set(), True

    monkeypatch.setattr(doctor, "_transaction_artifacts", incomplete)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert "transaction_artifact_state_unknown" in result["details"]["deletion_codes"]
    registry.release(lease)


def test_quarantine_filesystem_proof_has_no_live_sql_cursor(tmp_path, monkeypatch):
    import pytest
    from markdown_transaction import TransactionFailure

    root, state, coordinator, registry, lease = _environment(tmp_path)
    changes = [MarkdownChange.create("knowledge/notes/refused.md", b"# Refused\n")]
    prepared = coordinator.prepare(changes, operation_id="compile:refused-lock-check", preconditions={"knowledge/notes/absent.md": "0" * 64})
    with pytest.raises(TransactionFailure):
        coordinator.apply(prepared.id)
    observed = []

    def proof(self, database, identifier, committed_creates):
        registry.heartbeat(lease)
        observed.append(identifier)
        return False

    monkeypatch.setattr(doctor._CompiledDaySupersession, "resolves", proof)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert observed == [prepared.id]
    assert result["details"]["quarantined_unresolved"] == 1
    assert result["details"]["read_error"] is False
    registry.release(lease)


def test_revalidation_catches_change_in_late_quarantine_proof(tmp_path, monkeypatch):
    import pytest
    from markdown_transaction import TransactionFailure

    root, state, coordinator, registry, lease = _environment(tmp_path)
    prepared = coordinator.prepare([MarkdownChange.create("knowledge/notes/refused.md", b"# Refused\n")], operation_id="compile:refused-late-check", preconditions={"knowledge/notes/absent.md": "0" * 64})
    with pytest.raises(TransactionFailure):
        coordinator.apply(prepared.id)

    def proof(self, database, identifier, committed_creates):
        coordinator.prepare([MarkdownChange.create("knowledge/notes/late.md", b"# Late\n")], operation_id="late-during-proof")
        return False

    monkeypatch.setattr(doctor._CompiledDaySupersession, "resolves", proof)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic()+30, vault_root=root)
    assert result["details"]["read_error"] is True
    assert "transaction_state_corrupt" not in result["details"]["deletion_codes"]
    registry.release(lease)


def test_standalone_canonical_receipt_callback_releases_shared_lock(tmp_path, monkeypatch):
    import compile_memory as compiler

    from tests.test_native_compile_companion_adopted import _committed_companion

    root, coordinator, first, old, _, _ = _committed_companion(tmp_path, monkeypatch)
    registry = coordinator._ownership_registry()
    lease = registry.acquire("doctor", scope="global", actor_id="standalone-test", token="standalone-token")
    failures = []
    original = compiler.daily_is_compiled

    def inspect(logical, content, selector):
        assert selector.receipt(first)["operation_id"] == old["operation_id"]
        try:
            registry.heartbeat(lease)
        except sqlite3.Error as error:
            failures.append(str(error))
        return original(logical, content, selector)

    monkeypatch.setattr(compiler, "daily_is_compiled", inspect)
    reader = doctor._CompiledDaySupersession(root, coordinator.state_root, deadline=time.monotonic()+30)
    assert reader._standalone_day_compiled(first.logical_path, first.original_content) is False
    assert failures == []
    registry.release(lease)


def test_standalone_rejects_mutated_committed_operation_after_receipt_proof(tmp_path, monkeypatch):
    import compile_memory as compiler
    import pytest

    from tests.test_native_compile_companion_adopted import _committed_companion

    root, coordinator, first, old, _, _ = _committed_companion(tmp_path, monkeypatch)
    registry = coordinator._ownership_registry()
    transaction = coordinator.committed_attempt(old["operation_id"])
    original = compiler.daily_is_compiled

    def inspect(logical, content, selector):
        assert selector.receipt(first)["operation_id"] == old["operation_id"]
        with sqlite3.connect(registry.database_path) as database:
            database.execute("UPDATE operation SET after_hash=? WHERE transaction_id=?", ("0" * 64, transaction.id))
        return original(logical, content, selector)

    monkeypatch.setattr(compiler, "daily_is_compiled", inspect)
    reader = doctor._CompiledDaySupersession(root, coordinator.state_root, deadline=time.monotonic()+30)
    with pytest.raises(TimeoutError, match="snapshot changed"):
        reader._standalone_day_compiled(first.logical_path, first.original_content)


def test_expired_streaming_comparison_closes_snapshot_before_owner_write(tmp_path):
    from contextlib import closing

    import pytest

    root, state, _, registry, lease = _environment(tmp_path)
    with closing(doctor._readonly_database(registry.database_path, state)) as database:
        snapshot = doctor._read_transaction_snapshot(database, float("inf"))
        with pytest.raises(TimeoutError):
            doctor._require_transaction_snapshot(database, snapshot, time.monotonic()-1)
        assert database.in_transaction is False
        registry.heartbeat(lease)
    registry.release(lease)


@pytest.mark.parametrize("phase", ["read", "verify"])
def test_snapshot_deadline_expiring_at_commit_is_visible(tmp_path, monkeypatch, phase):
    from contextlib import closing, contextmanager

    root, state, _, registry, lease = _environment(tmp_path)
    deadline = time.monotonic()+30
    original = doctor._one_snapshot

    def expired_clock():
        return deadline+1

    with closing(doctor._readonly_database(registry.database_path, state)) as database:
        snapshot = doctor._read_transaction_snapshot(database, float("inf"))
        with monkeypatch.context() as patch:
            @contextmanager
            def expiring_snapshot(connection):
                with original(connection):
                    yield
                patch.setattr(doctor.time, "monotonic", expired_clock)

            patch.setattr(doctor, "_one_snapshot", expiring_snapshot)
            actions = {"read": (doctor._read_transaction_snapshot, (database, deadline)), "verify": (doctor._require_transaction_snapshot, (database, snapshot, deadline))}
            action, arguments = actions[phase]
            with pytest.raises(TimeoutError):
                action(*arguments)
        assert database.in_transaction is False
        registry.heartbeat(lease)
    registry.release(lease)
