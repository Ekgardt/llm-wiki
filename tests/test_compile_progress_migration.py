"""A candidate upgrade is atomic, preserves queue work and rejects old clients."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import compile_progress_migration as migration
import memory_queue
import pytest
from reliable_memory import OperationalDatabaseContractError

from tests.adopted_vault import adopt


@pytest.fixture
def candidate(tmp_path):
    root, state = adopt(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'daily': 'retained input'})
    target = state / 'run' / 'candidate.sqlite3'
    with contextlib.closing(sqlite3.connect(target)) as destination:
        with queue.connection() as source:
            source.backup(destination)
    target.chmod(0o600)
    return state, target


def _snapshot(path):
    with contextlib.closing(sqlite3.connect(path)) as database:
        return tuple(database.iterdump())


def _version(path):
    with contextlib.closing(sqlite3.connect(path)) as database:
        return database.execute('PRAGMA user_version').fetchone()[0]


def test_migration_preserves_work_and_blocks_old_clients(candidate):
    state, path = candidate
    before = _snapshot(state / 'run/queue-v3.sqlite3')
    report = migration.migrate_candidate(path, state_root=state)
    assert report['user_version'] == 4
    assert report['row_counts']['tasks'] == 1
    assert report['compile_progress'] == {'works': 0, 'events': 0, 'artifacts': 0}
    assert _snapshot(state / 'run/queue-v3.sqlite3') == before
    assert set(before).issubset(_snapshot(path))
    with pytest.raises(OperationalDatabaseContractError):
        memory_queue.require_queue_v3_openable(path, state_root=state)


def test_completed_migration_is_verified_and_reusable(candidate):
    state, path = candidate
    report = migration.migrate_candidate(path, state_root=state)
    before = _snapshot(path)
    assert migration.migrate_candidate(path, state_root=state) == report
    assert _snapshot(path) == before


@pytest.mark.parametrize('boundary', [
    'after_statement:0', 'after_statement:1', 'after_statement:2',
    'after_version', 'before_commit',
])
def test_interruption_rolls_back_all_schema_changes(candidate, boundary):
    state, path = candidate
    before = _snapshot(path)

    def interrupt(point):
        if point == boundary:
            raise OSError('injected interruption')

    with pytest.raises(OSError, match='injected interruption'):
        migration.migrate_candidate(path, state_root=state, killpoint=interrupt)
    assert _version(path) == 3
    assert _snapshot(path) == before
    assert migration.migrate_candidate(path, state_root=state)['user_version'] == 4


_CRASH_PROGRAM = '''
import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from compile_progress_migration import migrate_candidate
def crash(point):
    if point == sys.argv[4]:
        os._exit(17)
migrate_candidate(Path(sys.argv[2]), state_root=Path(sys.argv[3]), killpoint=crash)
'''


@pytest.mark.parametrize('boundary,version', [
    ('after_statement:0', 3), ('after_statement:1', 3),
    ('after_statement:2', 3), ('after_version', 3),
    ('before_commit', 3), ('after_commit', 4),
])
def test_process_death_recovers_one_complete_version(candidate, boundary, version):
    state, path = candidate
    result = subprocess.run([
        sys.executable, '-c', _CRASH_PROGRAM,
        str(Path(migration.__file__).parent), str(path), str(state), boundary,
    ], check=False)
    assert result.returncode == 17
    assert _version(path) == version
    assert migration.migrate_candidate(path, state_root=state)['user_version'] == 4


def test_active_database_is_never_a_candidate(candidate):
    state, _ = candidate
    path = state / 'run/queue-v3.sqlite3'
    before = _snapshot(path)
    with pytest.raises(ValueError, match='active'):
        migration.migrate_candidate(path, state_root=state)
    assert _snapshot(path) == before
    assert _version(path) == 3


def test_hardlink_to_active_database_is_rejected(candidate):
    state, path = candidate
    path.unlink()
    os.link(state / 'run/queue-v3.sqlite3', path)
    with pytest.raises(ValueError, match='active'):
        migration.migrate_candidate(path, state_root=state)
    assert _version(path) == 3


def test_missing_candidate_is_not_created(candidate):
    state, path = candidate
    path.unlink()
    with pytest.raises(OSError):
        migration.migrate_candidate(path, state_root=state)
    assert not path.exists()


@pytest.mark.parametrize('sql', [
    'PRAGMA application_id=1', 'PRAGMA user_version=99',
    'DROP TABLE attempt_history',
    "UPDATE tasks SET input_hash='" + '0' * 64 + "'",
])
def test_invalid_source_is_not_upgraded(candidate, sql):
    state, path = candidate
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute(sql)
        database.commit()
    before = _snapshot(path)
    with pytest.raises((ValueError, sqlite3.Error)):
        migration.migrate_candidate(path, state_root=state)
    assert _snapshot(path) == before


def test_new_version_requires_journal_protection(candidate):
    state, path = candidate
    migration.migrate_candidate(path, state_root=state)
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
    with pytest.raises(ValueError):
        migration.migrate_candidate(path, state_root=state)


def test_expired_deadline_cannot_start_migration(candidate):
    state, path = candidate
    before = _snapshot(path)
    with pytest.raises(TimeoutError):
        migration.migrate_candidate(path, state_root=state, deadline=0)
    assert _snapshot(path) == before


def test_sqlite_full_rolls_back_upgrade(candidate, monkeypatch):
    state, path = candidate
    before = _snapshot(path)
    original = migration.open_operational_db

    def no_room(*args, **kwargs):
        database = original(*args, **kwargs)
        pages = database.execute('PRAGMA page_count').fetchone()[0]
        database.execute(f'PRAGMA max_page_count={pages}')
        return database

    monkeypatch.setattr(migration, 'open_operational_db', no_room)
    with pytest.raises(sqlite3.OperationalError, match='full'):
        migration.migrate_candidate(path, state_root=state)
    assert _version(path) == 3
    assert _snapshot(path) == before


def test_unversioned_partial_journal_is_not_silently_adopted(candidate):
    state, path = candidate
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute('CREATE TABLE compile_work_events(unverified TEXT)')
    before = _snapshot(path)
    with pytest.raises(sqlite3.OperationalError, match='already exists'):
        migration.migrate_candidate(path, state_root=state)
    assert _version(path) == 3
    assert _snapshot(path) == before


def test_version_stamp_alone_is_not_a_valid_migration(candidate):
    state, path = candidate
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute('PRAGMA user_version=4')
    before = _snapshot(path)
    with pytest.raises(ValueError, match='schema'):
        migration.migrate_candidate(path, state_root=state)
    assert _snapshot(path) == before
