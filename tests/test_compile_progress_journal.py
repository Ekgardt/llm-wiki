"""Durable intermediate work is distinct from a terminal queue result."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import compile_progress as progress
import pytest


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(tmp_path / 'queue.sqlite3')
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA journal_mode=DELETE')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY)')
    connection.execute("INSERT INTO tasks VALUES('task')")
    for statement in progress.SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    yield connection
    connection.close()


def event(**changes):
    value = progress.CompileEvent(
        work_id='a' * 64, task_id='task', sequence=0,
        source_digest='b' * 64, stage='pending', outcome='ready',
        details={'policy_version': 'compile-test/v1'},
    )
    return replace(value, **changes)


def append(database, value):
    database.execute('BEGIN IMMEDIATE')
    try:
        progress.append_event(database, value)
        database.commit()
    except BaseException:
        database.rollback()
        raise


def test_checkpoint_survives_connection_restart(database):
    value = event()
    append(database, value)
    path = database.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as reopened:
        reopened.row_factory = sqlite3.Row
        assert progress.latest_event(reopened, value.work_id) == value


def test_old_sequence_cannot_overwrite_later_progress(database):
    append(database, event())
    later = event(sequence=1, stage='snapshot_ready')
    append(database, later)
    with pytest.raises(progress.ProgressConflict):
        append(database, event(sequence=1, stage='draft_ready'))
    assert progress.latest_event(database, later.work_id) == later


def test_failed_transaction_keeps_previous_checkpoint(database):
    append(database, event())
    database.execute('BEGIN IMMEDIATE')
    progress.append_event(database, event(sequence=1, stage='snapshot_ready'))
    database.rollback()
    assert progress.latest_event(database, 'a' * 64) == event()


def test_work_cannot_change_its_sources(database):
    append(database, event())
    with pytest.raises(progress.ProgressConflict):
        append(database, event(sequence=1, source_digest='c' * 64))


@pytest.mark.parametrize('sql', [
    "UPDATE compile_work_events SET stage='published'",
    'DELETE FROM compile_work_events',
    "DELETE FROM tasks WHERE id='task'",
])
def test_retained_progress_cannot_be_overwritten_or_orphaned(database, sql):
    append(database, event())
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(sql)
    database.rollback()
    assert progress.latest_event(database, 'a' * 64) == event()


def test_corrupted_checkpoint_is_not_returned_as_valid(database):
    append(database, event())
    database.execute('DROP TRIGGER compile_work_events_no_update')
    database.execute("UPDATE compile_work_events SET details_json='{}'")
    database.commit()
    with pytest.raises(progress.ProgressCorrupt):
        progress.latest_event(database, 'a' * 64)


def test_no_implicit_write_transaction(database):
    with pytest.raises(progress.ProgressConflict, match='transaction'):
        progress.append_event(database, event())


@pytest.mark.parametrize('changes', [
    {'sequence': True}, {'sequence': -1}, {'source_digest': 'x'},
    {'work_id': '../other'}, {'stage': 'made_up'}, {'outcome': 'green'},
    {'details': {'cost': float('nan')}},
])
def test_invalid_checkpoint_is_rejected(database, changes):
    with pytest.raises((ValueError, TypeError)):
        append(database, event(**changes))
    assert progress.latest_event(database, 'a' * 64) is None


def test_lookup_does_not_create_missing_schema(tmp_path):
    with sqlite3.connect(tmp_path / 'unmigrated.sqlite3') as connection:
        with pytest.raises(sqlite3.OperationalError):
            progress.latest_event(connection, 'a' * 64)
        assert connection.execute('SELECT name FROM sqlite_master').fetchall() == []


def test_positive_gap_cannot_skip_prior_checkpoint(database):
    with pytest.raises(progress.ProgressConflict):
        append(database, event(sequence=2))


@pytest.mark.parametrize('transaction_end,expected_sequence', [
    ('COMMIT', 1), ('SELECT 1', 0),
])
def test_process_death_preserves_only_committed_progress(
    database, transaction_end, expected_sequence,
):
    append(database, event())
    path = database.execute('PRAGMA database_list').fetchone()[2]
    script = '''
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from compile_progress import CompileEvent, append_event
connection = sqlite3.connect(sys.argv[2])
connection.row_factory = sqlite3.Row
connection.execute('PRAGMA foreign_keys=ON')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
append_event(connection, CompileEvent('a'*64, 'task', 1, 'b'*64,
    'snapshot_ready', 'ready', {}))
connection.execute(sys.argv[3])
os._exit(17)
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(Path(progress.__file__).parent),
         path, transaction_end], check=False,
    )
    assert result.returncode == 17
    assert progress.latest_event(database, 'a' * 64).sequence == expected_sequence


@pytest.mark.parametrize('details', [{'items': (1, 2)}, {1: 'not a JSON key'}])
def test_non_json_values_cannot_change_when_restored(database, details):
    with pytest.raises(ValueError, match='lossless'):
        append(database, event(details=details))
