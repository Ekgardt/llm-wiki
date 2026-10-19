"""Qualify the approved queue schema upgrade on an unpublished database copy.

This module never publishes a database or edits adoption records. The offline
installer still needs a coherent backup, maintenance fence and recoverable
publication before normal clients may use the new schema. Version-3 runtime
clients intentionally continue to reject the resulting version-4 candidate.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import compile_progress
import memory_queue
from reliable_memory import (
    DEFAULTS,
    OperationalDatabaseContractError,
    begin_immediate,
    open_operational_db,
    operational_deadline_active,
    validate_operational_db_file,
)

# Schema identity, not an operational resource limit. The coordinator stays v3.
COMPILE_QUEUE_VERSION = 4


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


def _upgrade_transaction(database, state_root, emit, deadline):
    with begin_immediate(database):
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
