"""Qualified candidate migration and explicit offline upgrade/recovery.

Offline operations require all agent processes stopped as well as the canonical
maintenance fence. This is not an online migration of already-open SQL handles.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import compile_progress
import installed_memory_repair as repair
import memory_queue
import private_vault_backup as backup
from reliable_memory import (
    DEFAULTS,
    OperationalDatabaseContractError,
    RuntimeFileIdentity,
    _require_runtime_parent_containment,
    begin_immediate,
    canonical_json_bytes,
    capture_runtime_file_identity,
    open_operational_db,
    operational_deadline_active,
    publish_runtime_file,
    read_runtime_bytes,
    sha256_bytes,
    validate_operational_db_file,
)

# Schema identity, not an operational resource limit. The coordinator stays v3.
COMPILE_QUEUE_VERSION = compile_progress.QUEUE_SCHEMA_VERSION


def _require_candidate(path: Path, state_root: Path) -> None:
    validate_operational_db_file(path, state_root, max_bytes=None, owner_only=True)
    active = state_root / 'run/queue-v3.sqlite3'
    if active.exists() and path.samefile(active):
        raise ValueError('active queue cannot be used as a migration candidate')


def _require_queue_base(database) -> None:
    if not memory_queue._queue_v3_schema_complete(database):
        raise ValueError('candidate queue schema is incomplete')
    if not memory_queue._queue_v3_invariants_hold(database):
        raise ValueError('candidate queue invariants failed')


def _source_version(database) -> int:
    identity = database.execute('PRAGMA application_id').fetchone()[0]
    version = database.execute('PRAGMA user_version').fetchone()[0]
    if identity != memory_queue._QUEUE_V3_CONTRACT.application_id:
        raise OperationalDatabaseContractError('candidate is not a queue database')
    if version not in (memory_queue._QUEUE_V3_CONTRACT.user_version, COMPILE_QUEUE_VERSION):
        raise OperationalDatabaseContractError('unsupported queue migration source version')
    return version


def _apply_journal(database, emit, deadline) -> None:
    for index, sql in enumerate(compile_progress.SCHEMA_STATEMENTS):
        operational_deadline_active(deadline)
        database.execute(sql)
        emit(f'after_statement:{index}')
    database.execute(f'PRAGMA user_version={COMPILE_QUEUE_VERSION}')
    emit('after_version')


def _certify_target(database, state_root, deadline):
    compile_progress._require_schema(database)
    counts = memory_queue.audit_compile_progress(database, state_root, deadline=deadline)
    report = memory_queue._queue_v3_report(database)
    report['compile_progress'] = counts
    return report


def _upgrade_transaction(database, state_root, emit, deadline, before_commit=None):
    with begin_immediate(database, before_commit=before_commit):
        version = _source_version(database)
        _require_queue_base(database)
        if version != COMPILE_QUEUE_VERSION:
            _apply_journal(database, emit, deadline)
        report = _certify_target(database, state_root, deadline)
        emit('before_commit')
        operational_deadline_active(deadline)
    emit('after_commit')
    return report


def migrate_candidate(path: Path, *, state_root: Path, deadline=None, killpoint=None):
    """Atomically upgrade a separate candidate, or verify an already-upgraded copy."""
    operational_deadline_active(deadline)
    path, state_root = Path(path), Path(state_root)
    _require_candidate(path, state_root)
    emit = killpoint or (lambda _point: None)
    with closing(open_operational_db(
        path, busy_ms=DEFAULTS.queue_busy_ms, deadline=deadline,
    )) as database:
        return _upgrade_transaction(database, state_root, emit, deadline)


def _candidate_has_current_schema(path, state_root):
    if not path.exists():
        return False
    validate_operational_db_file(path, state_root, max_bytes=None, owner_only=True)
    with closing(open_operational_db(path, busy_ms=DEFAULTS.queue_busy_ms)) as database:
        return database.execute("PRAGMA user_version").fetchone()[0] == COMPILE_QUEUE_VERSION


def _require_fresh_candidate_path(path, state_root):
    _require_runtime_parent_containment(path.parent.resolve(strict=True),
                                        Path(state_root).resolve(strict=True))
    if path.absolute() == (state_root / "run/queue-v3.sqlite3").absolute():
        raise ValueError("active queue cannot be a fresh candidate")
    if path.exists():
        _require_candidate(path, state_root)


def initialize_fresh_candidate(path, *, state_root):
    """Build the current journal only on an unpublished empty adoption candidate."""
    path = Path(path)
    _require_fresh_candidate_path(path, state_root)
    if not _candidate_has_current_schema(path, state_root):
        memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    report = migrate_candidate(path, state_root=state_root)
    with closing(open_operational_db(path, busy_ms=DEFAULTS.queue_busy_ms)) as database:
        memory_queue._require_empty_queue_v3(database)
    return report


def _require_owner(registry, lease):
    with closing(registry._connect()) as database:
        registry.require(database, lease)


def _backup_digest(image):
    backup.validate_backup_image(image)
    raw, _manifest = backup._read_manifest(image)
    return sha256_bytes(raw)


def _upgrade_envelope(state, previous, image):
    active = repair._artifact_record(state / 'run/queue-v3.sqlite3', state, None)
    # _build_image has just validated this image under the same owner.
    raw, _manifest = backup._read_manifest(image)
    manifest_digest = sha256_bytes(raw)
    identity = {'previous': previous, 'queue_active': active, 'backup': manifest_digest}
    return {
        'schema_version': 'reliability-v3-adoption/v2', 'previous': previous,
        'previous_sha256': sha256_bytes(canonical_json_bytes(previous)),
        'queue_active': active, 'queue_schema_sha256': memory_queue.QUEUE_COMPILE_SCHEMA_SHA256,
        'operation_id': sha256_bytes(canonical_json_bytes(identity)),
        'backup_manifest_sha256': manifest_digest,
    }


def _publish_intent(state, record):
    repair._validate_upgrade_envelope(record)
    path = state / 'run/reliability-v3-adopted.json'
    publish_runtime_file(
        path, canonical_json_bytes(record), state_root=state, create_only=False,
        expected=capture_runtime_file_identity(path, state_root=state),
        expected_sha256=record['previous_sha256'],
    )


def _prepare_upgrade(root, state, image, previous, registry, lease, emit):
    root, state, image = backup._resolve_roots(root, state, image)
    repair.require_reliability_v3_adopted(root=root, state_root=state)
    backup._build_image(root=root, state_root=state, image=image, lease=lease,
                        now=datetime.now(timezone.utc), deadline=float('inf'))
    emit('after_backup')
    record = _upgrade_envelope(state, previous, image)
    _require_owner(registry, lease)
    _publish_intent(state, record)
    emit('after_intent')
    return record


def _verify_backup_binding(root, state, image, record):
    backup._validate_root_locations(root, state, image)
    if _backup_digest(image) != record['backup_manifest_sha256']:
        raise ValueError('migration backup digest changed')
    _raw, manifest = backup._read_manifest(image)
    expected = {'vault_sha256': sha256_bytes(str(root).encode()),
                'state_sha256': sha256_bytes(str(state).encode())}
    if manifest['source_roots'] != expected:
        raise ValueError('migration backup belongs to different roots')
    old = read_runtime_bytes(image / 'state/run/reliability-v3-adopted.json', image,
                             max_bytes=repair._MAX_RECORD_BYTES, owner_only=True)
    _require_previous_backup(old, record)


def _require_previous_backup(old, record):
    if old != canonical_json_bytes(record['previous']):
        raise ValueError('migration backup adoption evidence differs')


def _resume_validator(record, *, queue_version, database_name, **kwargs):
    if database_name == 'queue':
        record = dict(record, user_version=queue_version)
    repair._validate_active_database_reference(record, database_name=database_name, **kwargs)


def _recover_queue_version(state, record):
    path = state / 'run/queue-v3.sqlite3'
    repair._validate_artifact_reference(record['queue_active'], expected_path=path,
                                       state_root=state, mutable=True, max_bytes=None)
    # A write-capable SQLite open recovers an interrupted rollback journal.
    # It is allowed only after backup/identity verification and exclusive admission.
    with closing(open_operational_db(path, busy_ms=DEFAULTS.queue_busy_ms)) as database:
        return _source_version(database)


def _require_pre_upgrade_bytes(state, record, version):
    if version == COMPILE_QUEUE_VERSION:
        return
    current = repair._artifact_record(state / 'run/queue-v3.sqlite3', state, None)
    if current != record['queue_active']:
        # SQLite rollback can legitimately change mtime; identity and bytes must match.
        _require_pre_upgrade_digest(current, record['queue_active'])


def _require_pre_upgrade_digest(current, expected):
    repair._validate_artifact_identity(expected, RuntimeFileIdentity(**current['identity']), True)
    if current['sha256'] != expected['sha256']:
        raise ValueError('migration queue preimage changed')


def _finish_upgrade(root, state, record, registry, lease, emit):
    _require_owner(registry, lease)
    version = _recover_queue_version(state, record)
    repair._load_complete_adoption(root=root, state_root=state,
        active_validator=partial(_resume_validator, queue_version=version))
    _require_pre_upgrade_bytes(state, record, version)
    with closing(open_operational_db(
        state / 'run/queue-v3.sqlite3', busy_ms=DEFAULTS.queue_busy_ms,
    )) as database:
        _upgrade_transaction(database, state, emit, None,
                             before_commit=partial(_require_owner, registry, lease))
    _require_owner(registry, lease)
    return repair.require_reliability_v3_adopted(root=root, state_root=state)


def _prepare_or_resume(root, state, image, record, registry, lease, emit):
    if record['schema_version'] == 'reliability-v3-adoption/v1':
        return _prepare_upgrade(root, state, image, record, registry, lease, emit)
    _verify_backup_binding(root, state, image, record)
    return record


def upgrade_offline(*, root: Path, state_root: Path, backup_image: Path,
                    confirm_all_agents_stopped: bool = False, killpoint=None):
    """Upgrade or resume under the canonical maintenance fence; retain the backup.

    The caller supplies an external empty directory for a new upgrade, or the
    exact retained image for recovery. No automatic rollback or model calls occur.
    A failed partial backup is retained; before intent publication, retry requires
    another empty directory. Once intent exists, recovery uses the bound backup.
    """
    if confirm_all_agents_stopped is not True:
        raise ValueError('offline migration requires all agents stopped')
    root = backup._resolve_directory(Path(root), 'migration_root_invalid')
    state = backup._resolve_directory(Path(state_root), 'migration_state_invalid')
    image = backup._resolve_staging(Path(backup_image))
    emit = killpoint or (lambda _point: None)
    with backup._held_backup_owner(state) as (registry, lease), backup._heartbeat(registry, lease):
        record = repair._read_adoption_record(state / 'run/reliability-v3-adopted.json', state)
        if record['schema_version'] == 'reliability-v3-adoption/v3':
            _require_owner(registry, lease)
            return repair.require_reliability_v3_adopted(root=root, state_root=state)
        record = _prepare_or_resume(root, state, image, record, registry, lease, emit)
        return _finish_upgrade(root, state, record, registry, lease, emit)
