"""New queue readers require coherent adoption evidence, not just a version stamp."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

import compile_progress as progress
import compile_progress_migration as migration
import doctor
import installed_memory_repair as repair
import memory_queue
import private_vault_backup
import pytest
from reliable_memory import canonical_json_bytes, validate_schema

from tests.adopted_vault import adopt_legacy as adopt


def _write_record(state, value):
    (state / 'run/reliability-v3-adopted.json').write_bytes(canonical_json_bytes(value))


@pytest.fixture
def upgraded(tmp_path):
    root, state = adopt(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'daily': 'retained'})
    candidate = state / 'run/test-copy.sqlite3'
    with contextlib.closing(sqlite3.connect(candidate)) as destination:
        with queue.connection() as source:
            source.backup(destination)
    candidate.chmod(0o600)
    migration.migrate_candidate(candidate, state_root=state)
    previous = json.loads((state / 'run/reliability-v3-adopted.json').read_bytes())
    os.replace(candidate, queue.db_path)
    active = repair._artifact_record(queue.db_path, state, repair._MAX_OPERATIONAL_DB_BYTES)
    schema = {'base': memory_queue.QUEUE_V3_SCHEMA_SHA256, 'version': 4,
              'journal': list(progress.SCHEMA_STATEMENTS)}
    record = {
        'schema_version': 'reliability-v3-adoption/v2', 'previous': previous,
        'previous_sha256': hashlib.sha256(canonical_json_bytes(previous)).hexdigest(),
        'queue_active': active,
        'queue_schema_sha256': hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        'operation_id': 'a' * 64, 'backup_manifest_sha256': 'b' * 64,
    }
    _write_record(state, record)
    return root, state, record


def test_version_two_admission_preserves_original_evidence(upgraded):
    root, state, record = upgraded
    assert repair.require_reliability_v3_adopted(root=root, state_root=state) == record
    validate_schema(record['previous'], repair._ADOPTION_SCHEMA)
    with pytest.raises(ValueError):
        validate_schema(record, repair._ADOPTION_SCHEMA)


def test_normal_queue_lifecycle_uses_version_four(upgraded):
    root, state, _ = upgraded
    queue = memory_queue.active_memory_queue(root, state)
    lease = queue.claim('migration-test')
    lease = queue.heartbeat(lease)
    saved = queue.save_compile_artifact(lease, progress.CompileEvent(
        'c' * 64, lease.id, 0, 'd' * 64, 'pending', 'ready', {},
    ), b'answer')
    assert queue.read_compile_artifact(saved) == b'answer'
    with pytest.raises(memory_queue.QueueOperationError, match='compile_work_incomplete'):
        queue.publish_result(lease, operation_id='migration-test-result', result=b'final')
    queue.cancel(lease.id)
    queue.enqueue('query', 1, {'query': 'migration lifecycle'})
    lease = queue.claim('migration-test')
    queue.publish_result(lease, operation_id='migration-test-result', result=b'final')
    assert queue.acknowledge(lease).state == 'succeeded'


def test_health_backup_and_deletion_accept_the_same_contract(upgraded):
    root, state, _ = upgraded
    assert memory_queue.validate_queue_v3_database(
        state / 'run/queue-v3.sqlite3', state_root=state,
    )['user_version'] == 4
    assert private_vault_backup._validated_databases(state)[1]['user_version'] == 4
    assert 'queue_task_retained' in repair._queue_database_blockers(state, math.inf)
    result = doctor._queue_v2_check(state, datetime.now(timezone.utc), math.inf)
    assert 'queue_state_unreadable' not in result['details']['deletion_codes']


def test_old_adoption_record_cannot_admit_version_four(upgraded):
    root, state, record = upgraded
    _write_record(state, record['previous'])
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


def test_new_adoption_record_cannot_admit_version_three(upgraded):
    root, state, _ = upgraded
    with contextlib.closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        database.execute('PRAGMA user_version=3')
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


@pytest.mark.parametrize('field', ['previous_sha256', 'queue_schema_sha256'])
def test_changed_evidence_digest_refuses_admission(upgraded, field):
    root, state, record = upgraded
    record[field] = '0' * 64
    _write_record(state, record)
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


def test_old_tombstone_is_still_verified(upgraded):
    root, state, _ = upgraded
    (state / 'run/queue.sqlite3').write_bytes(b'changed')
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


def test_missing_journal_protection_refuses_admission(upgraded):
    root, state, _ = upgraded
    with contextlib.closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


def test_full_adoption_certification_checks_retained_answers(upgraded):
    root, state, _ = upgraded
    queue = memory_queue.active_memory_queue(root, state)
    lease = queue.claim('migration-test')
    saved = queue.save_compile_artifact(lease, progress.CompileEvent(
        'c' * 64, lease.id, 0, 'd' * 64, 'pending', 'ready', {},
    ), b'answer')
    (state / saved.details['artifact']['reference']).write_bytes(b'changed')
    with pytest.raises(repair.ReliabilityV3ValidationError):
        repair.require_reliability_v3_adopted(root=root, state_root=state)
    # Ordinary admission verifies contracts; full retained history is a separate check.
    assert memory_queue.active_memory_queue(root, state).get(lease.id).id == lease.id


@pytest.mark.parametrize('reopen', [False, True])
def test_admitted_queue_cannot_silently_change_version(upgraded, reopen):
    root, state, _ = upgraded
    queue = memory_queue.active_memory_queue(root, state)
    with contextlib.closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        database.execute('PRAGMA user_version=3')
    with pytest.raises((repair.ReliabilityV3ValidationError, sqlite3.DatabaseError)):
        if reopen:
            queue = memory_queue.active_memory_queue(root, state)
        queue.enqueue('compile', 1, {'daily': 'must refuse'})


def test_markdown_transactions_keep_working_with_upgraded_queue(upgraded):
    from markdown_transaction import active_markdown_coordinator, mutate_owned_knowledge

    root, state, _ = upgraded
    page = root / 'knowledge/notes/fixture.md'
    page.parent.mkdir(parents=True)
    coordinator = active_markdown_coordinator(root, state)
    with coordinator.writer_gate() as owner:
        record = mutate_owned_knowledge(
            coordinator, owner, 'upgraded-queue-write', {page: b'---\ntype: concept\n---\nfixture\n'},
        )
    assert record.state == 'committed'
    assert page.read_bytes().endswith(b'fixture\n')


def test_full_backup_image_carries_upgraded_queue(upgraded, tmp_path):
    root, state, record = upgraded
    staging = tmp_path / 'staging'
    staging.mkdir()
    with private_vault_backup.staged_backup_image(
        root=root, state_root=state, staging_parent=staging,
    ) as image:
        assert private_vault_backup.validate_backup_image(image)['database_count'] == 2
        copied = json.loads((image / 'state/run/reliability-v3-adopted.json').read_bytes())
        assert copied == record
        assert private_vault_backup._validated_databases(image / 'state')[1]['user_version'] == 4
