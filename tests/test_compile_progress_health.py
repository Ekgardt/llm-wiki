"""Full health and backup certification include intermediate compile evidence."""
import math
from datetime import UTC, datetime

import compile_progress as progress
import private_vault_backup
import pytest
import queue_core
import reliability_v3_validation
from llm_wiki import adoption_admission, doctor_queue, queue_v3_readers

from tests.adopted_vault import adopt


@pytest.fixture
def stored(tmp_path):
    root, state = adopt(tmp_path)
    queue = queue_core.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'daily': 'fixture'})
    lease = queue.claim('worker')
    event = progress.CompileEvent('a'*64, lease.id, 0, 'b'*64, 'pending', 'ready', {})
    saved = queue.save_compile_artifact(lease, event, b'answer')
    return queue, saved


def test_full_certification_counts_compile_evidence(stored):
    queue, saved = stored
    report = queue_core.validate_queue_v3_database(queue.db_path, state_root=queue.state_root)
    assert report['compile_progress'] == {'events': 1, 'works': 1, 'artifacts': 1}


def test_full_certification_rejects_missing_intermediate_answer(stored):
    queue, saved = stored
    (queue.state_root / saved.details['artifact']['reference']).unlink()
    with pytest.raises(ValueError):
        queue_core.validate_queue_v3_database(queue.db_path, state_root=queue.state_root)


def test_full_doctor_names_corrupt_compile_evidence(stored):
    queue, saved = stored
    (queue.state_root / saved.details['artifact']['reference']).unlink()
    result = doctor_queue.queue_v2_check(queue.state_root, datetime.now(UTC), math.inf)
    assert result['status'] == 'error'
    assert 'compile' in result['message'].lower()
    assert result['details']['compile_progress_error']


def test_full_doctor_retains_compile_history(stored):
    queue, saved = stored
    result = doctor_queue.queue_v2_check(queue.state_root, datetime.now(UTC), math.inf)
    assert 'queue_compile_progress_retained' in result['details']['deletion_codes']
    assert result['details']['compile_progress']['events'] == 1


def test_schema_guard_is_checked(stored):
    queue, saved = stored
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
    with pytest.raises(ValueError):
        queue_core.validate_queue_v3_database(queue.db_path, state_root=queue.state_root)


def test_audit_rejects_gap_in_history(stored):
    queue, saved = stored
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute('UPDATE compile_work_events SET sequence=2')
        database.execute(progress.SCHEMA_STATEMENTS[1])
    with pytest.raises(ValueError):
        queue_core.validate_queue_v3_database(queue.db_path, state_root=queue.state_root)


def test_backup_certification_checks_intermediate_bytes(stored):
    queue, saved = stored
    coordinator, result = private_vault_backup.validated_databases(queue.state_root)
    assert result['compile_progress']['artifacts'] == 1
    (queue.state_root / saved.details['artifact']['reference']).write_bytes(b'changed')
    with pytest.raises(private_vault_backup.BackupError, match='database_validation_failed'):
        private_vault_backup.validated_databases(queue.state_root)


def test_full_audit_honours_expired_deadline(stored):
    queue, saved = stored
    with queue.connection() as database:
        with pytest.raises(TimeoutError):
            queue_v3_readers.audit_compile_progress(database, queue.state_root, deadline=0)


def test_runtime_deletion_guard_includes_compile_history(stored):
    queue, saved = stored
    blockers = reliability_v3_validation.queue_database_blockers(queue.state_root, math.inf)
    assert 'queue_compile_progress_retained' in blockers


def test_runtime_deletion_guard_rejects_missing_compile_answer(stored):
    queue, saved = stored
    (queue.state_root / saved.details['artifact']['reference']).unlink()
    with pytest.raises(ValueError):
        reliability_v3_validation.queue_database_blockers(queue.state_root, math.inf)


def test_page_based_queue_scan_does_not_use_whole_file_read_cap(stored, monkeypatch):
    queue, saved = stored
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    assert 'queue_task_retained' in reliability_v3_validation.queue_database_blockers(queue.state_root, math.inf)


def test_page_based_coordinator_scan_does_not_use_whole_file_read_cap(stored, monkeypatch):
    queue, _ = stored
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    blockers, known, retained = reliability_v3_validation.coordinator_database_blockers(
        queue.state_root, datetime.now(UTC), math.inf, None,
    )
    assert 'transaction_state_unreadable' not in blockers


def test_page_based_blackboard_scan_does_not_use_whole_file_read_cap(stored, monkeypatch):
    queue, _ = stored
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    monkeypatch.setattr(adoption_admission, 'MAX_OPERATIONAL_DB_BYTES', 1)
    assert reliability_v3_validation.blackboard_claim_blockers(
        state_root=queue.state_root, now=datetime.now(UTC), deadline=math.inf,
    ) == []


def test_backup_and_doctor_certify_streamed_artifact_above_small_result_cap(stored):
    from dataclasses import replace

    queue, saved = stored
    row = queue.get(saved.task_id)
    lease = queue_core.QueueLease(
        id=row.id, kind=row.kind, handler_version=row.handler_version,
        payload=row.payload, input_hash=row.input_hash, owner=row.lease_owner,
        token=row.lease_token, expires_at=row.lease_expires_at, attempt=row.attempts,
        created_at=row.created_at, last_attempt_at=row.last_attempt_at,
        prior_attempts=row.attempts - 1,
    )
    block = b'x' * (1024 * 1024)
    queue.save_compile_artifact_stream(
        lease, replace(saved, sequence=1, details={}), (block for _ in range(20)))
    _, report = private_vault_backup.validated_databases(queue.state_root)
    assert report['compile_progress']['artifacts'] == 2
    result = doctor_queue.queue_v2_check(queue.state_root, datetime.now(UTC), math.inf)
    assert result['details']['compile_progress']['artifacts'] == 2
