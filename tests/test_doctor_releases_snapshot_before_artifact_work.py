"""Doctor filesystem inspection must not lock the adopted owner registry."""
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime

import doctor
import pytest
from llm_wiki import doctor_base, doctor_transactions
from markdown_transaction import MarkdownChange

from tests.compile_paths import point_compile_path
from tests.doctor_releases_snapshot_before_artifact_work_helpers import _environment
from tests.slow_machine import LONG_TIMEOUT


def test_full_capture_scan_waits_for_a_commit_within_its_deadline(tmp_path):
    from reliability_v3_validation import (
        capture_intent_rows,
    )

    state = tmp_path.resolve()
    path = state / "queue.sqlite3"
    ready, start, querying, done = (threading.Event() for _ in range(4))
    rows, errors = [], []

    def trace(query):
        if query.startswith("SELECT"):
            querying.set()

    def read():
        deadline = time.monotonic() + LONG_TIMEOUT
        with closing(doctor_base.readonly_database(path, state, deadline=deadline)) as database:
            database.set_trace_callback(trace)
            ready.set()
            assert start.wait(LONG_TIMEOUT)
            try:
                rows.extend(capture_intent_rows(database, deadline))
            except sqlite3.OperationalError as error:
                errors.append(error)
            finally:
                done.set()

    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("CREATE TABLE capture_intents(intent_id TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO capture_intents VALUES ('retained')")
        worker = threading.Thread(target=read)
        worker.start()
        try:
            assert ready.wait(LONG_TIMEOUT)
            writer.execute("BEGIN EXCLUSIVE")
            start.set()
            assert querying.wait(LONG_TIMEOUT)
            done.wait(2 * doctor_base.READ_BUSY_MS / 1000)
        finally:
            writer.rollback()
            start.set()
            worker.join(LONG_TIMEOUT)
    assert not worker.is_alive()
    assert errors == []
    assert [row["intent_id"] for row in rows] == ["retained"]


def test_capture_batch_lock_wait_shrinks_with_remaining_deadline(tmp_path, monkeypatch):
    import reliability_v3_validation

    state = tmp_path.resolve()
    path = state / "queue.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("CREATE TABLE capture_intents(intent_id TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO capture_intents VALUES ('retained')")
    with closing(doctor_base.readonly_database(path, state)) as database:
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: 100.0)
            assert next(reliability_v3_validation.capture_intent_rows(database, 100.125))["intent_id"] == "retained"
            wait_ms = database.execute("PRAGMA busy_timeout").fetchone()[0]
    assert wait_ms == 125


def test_default_busy_owner_heartbeat_and_release_during_artifact_work(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)
    failures = []
    original = doctor_transactions.checked_artifacts

    def inspect(*args):
        try:
            registry.heartbeat(lease)
            registry.release(lease)
        except sqlite3.Error as error:
            failures.append(str(error))
        return original(*args)

    monkeypatch.setattr(doctor_transactions, "checked_artifacts", inspect)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
    assert failures == []
    assert result["status"] == "ok"
    assert result["details"]["live_maintenance_owners"] == 0


def test_changed_transaction_after_snapshot_is_unreadable_not_healthy(tmp_path, monkeypatch):
    root, state, coordinator, registry, lease = _environment(tmp_path)
    original = doctor_transactions.checked_artifacts

    def inspect(*args):
        coordinator.prepare([MarkdownChange.create("knowledge/notes/new.md", b"# New\n")], operation_id="new-during-inspection")
        return original(*args)

    monkeypatch.setattr(doctor_transactions, "checked_artifacts", inspect)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    assert "transaction_state_corrupt" not in result["details"]["deletion_codes"]
    registry.release(lease)


def test_live_owner_is_still_reported_after_unlocked_inspection(tmp_path):
    root, state, _, registry, lease = _environment(tmp_path)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
    assert result["details"]["live_maintenance_owners"] == 1
    assert "maintenance_owner_live" in result["details"]["deletion_codes"]
    registry.release(lease)


def test_deadline_during_artifact_work_is_visible_and_owner_can_release(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)

    def inspect(*args):
        raise TimeoutError("controlled artifact deadline")

    monkeypatch.setattr(doctor_transactions, "checked_artifacts", inspect)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
    assert result["status"] == "error"
    assert result["details"]["read_error"] is True
    registry.release(lease)


def test_incomplete_artifact_inventory_never_permits_deletion(tmp_path, monkeypatch):
    root, state, _, registry, lease = _environment(tmp_path)
    def incomplete(*args):
        return set(), True

    monkeypatch.setattr(doctor_transactions, "transaction_artifacts", incomplete)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
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

    monkeypatch.setattr(doctor_transactions.CompiledDaySupersession, "resolves", proof)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
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

    monkeypatch.setattr(doctor_transactions.CompiledDaySupersession, "resolves", proof)
    result = doctor_transactions.transaction_check(state, datetime.now(UTC), time.monotonic()+30, vault_root=root)
    assert result["details"]["read_error"] is True
    assert "transaction_state_corrupt" not in result["details"]["deletion_codes"]
    registry.release(lease)


def test_standalone_canonical_receipt_callback_releases_shared_lock(tmp_path, monkeypatch):
    import compile_pipeline

    from tests.native_compile_companion_adopted_helpers import _committed_companion

    root, coordinator, first, old, _, _ = _committed_companion(tmp_path, monkeypatch)
    registry = coordinator._ownership_registry()
    lease = registry.acquire("doctor", scope="global", actor_id="standalone-test", token="standalone-token")
    failures = []
    original = compile_pipeline.daily_is_compiled

    def inspect(logical, content, selector):
        assert selector.receipt(first)["operation_id"] == old["operation_id"]
        try:
            registry.heartbeat(lease)
        except sqlite3.Error as error:
            failures.append(str(error))
        return original(logical, content, selector)

    point_compile_path(monkeypatch, "daily_is_compiled", inspect)
    reader = doctor_transactions.CompiledDaySupersession(root, coordinator.state_root, deadline=time.monotonic()+LONG_TIMEOUT)
    assert reader._standalone_day_compiled(first.logical_path, first.original_content) is False
    assert failures == []
    registry.release(lease)


def test_standalone_rejects_mutated_committed_operation_after_receipt_proof(tmp_path, monkeypatch):
    import compile_pipeline
    import pytest

    from tests.native_compile_companion_adopted_helpers import _committed_companion

    root, coordinator, first, old, _, _ = _committed_companion(tmp_path, monkeypatch)
    registry = coordinator._ownership_registry()
    transaction = coordinator.committed_attempt(old["operation_id"])
    original = compile_pipeline.daily_is_compiled

    def inspect(logical, content, selector):
        assert selector.receipt(first)["operation_id"] == old["operation_id"]
        with sqlite3.connect(registry.database_path) as database:
            database.execute("UPDATE operation SET after_hash=? WHERE transaction_id=?", ("0" * 64, transaction.id))
        return original(logical, content, selector)

    point_compile_path(monkeypatch, "daily_is_compiled", inspect)
    reader = doctor_transactions.CompiledDaySupersession(root, coordinator.state_root, deadline=time.monotonic()+LONG_TIMEOUT)
    with pytest.raises(TimeoutError, match="snapshot changed"):
        reader._standalone_day_compiled(first.logical_path, first.original_content)


def test_expired_streaming_comparison_closes_snapshot_before_owner_write(tmp_path):
    from contextlib import closing

    import pytest

    root, state, _, registry, lease = _environment(tmp_path)
    with closing(doctor_base.readonly_database(registry.database_path, state)) as database:
        snapshot = doctor_transactions.read_transaction_snapshot(database, float("inf"))
        with pytest.raises(TimeoutError):
            doctor_transactions.require_transaction_snapshot(database, snapshot, time.monotonic()-1)
        assert database.in_transaction is False
        registry.heartbeat(lease)
    registry.release(lease)


def _expect_timeout_once_snapshot_expires(monkeypatch, original, expired_clock, action, arguments):
    from contextlib import contextmanager

    with monkeypatch.context() as patch:
        @contextmanager
        def expiring_snapshot(connection):
            with original(connection):
                yield
            patch.setattr(doctor.time, "monotonic", expired_clock)

        patch.setattr(doctor_transactions, "one_snapshot", expiring_snapshot)
        with pytest.raises(TimeoutError):
            action(*arguments)


@pytest.mark.parametrize("phase", ["read", "verify"])
def test_snapshot_deadline_expiring_at_commit_is_visible(tmp_path, monkeypatch, phase):
    from contextlib import closing

    root, state, _, registry, lease = _environment(tmp_path)
    deadline = time.monotonic()+30
    original = doctor_transactions.one_snapshot

    def expired_clock():
        return deadline+1

    with closing(doctor_base.readonly_database(registry.database_path, state)) as database:
        snapshot = doctor_transactions.read_transaction_snapshot(database, float("inf"))
        actions = {"read": (doctor_transactions.read_transaction_snapshot, (database, deadline)), "verify": (doctor_transactions.require_transaction_snapshot, (database, snapshot, deadline))}
        action, arguments = actions[phase]
        _expect_timeout_once_snapshot_expires(monkeypatch, original, expired_clock, action, arguments)
        assert database.in_transaction is False
        registry.heartbeat(lease)
    registry.release(lease)
