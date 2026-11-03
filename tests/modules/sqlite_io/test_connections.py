"""Real connections retain ownership, storage mode and streaming semantics."""
import sqlite3

import pytest
from llm_wiki import sqlite_io


def test_writable_connection_remains_owned_by_caller(tmp_path):
    path = tmp_path / 'db.sqlite3'
    db = sqlite_io.open_operational_db(path, busy_ms=0)
    try:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
        db.execute('CREATE TABLE data(value)')
        db.execute('INSERT INTO data VALUES (42)')
        assert not (tmp_path / 'run').exists()
    finally:
        db.close()
    reader = sqlite_io.open_readonly_operational_db(path, tmp_path, max_bytes=None)
    try:
        assert reader.execute('SELECT value FROM data').fetchone()[0] == 42
        with pytest.raises(sqlite3.OperationalError):
            reader.execute('DELETE FROM data')
    finally:
        reader.close()


def test_failed_configuration_closes_connection(tmp_path, monkeypatch):
    opened = []

    def refuse(connection, *args, **kwargs):
        opened.append(connection)
        raise PermissionError('configuration refused')

    monkeypatch.setattr(sqlite_io, '_configure_operational_connection', refuse)
    with pytest.raises(PermissionError, match='configuration refused'):
        sqlite_io.open_operational_db(tmp_path / 'db.sqlite3', busy_ms=0)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


def test_streamed_scan_has_no_total_row_ceiling_and_checks_stop():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE data(value)')
        count = sqlite_io.OPERATIONAL_SCAN_BATCH_ROWS + 1
        db.executemany('INSERT INTO data VALUES (?)', ((i,) for i in range(count)))
        stops = []
        rows = list(sqlite_io.streamed_rows(db, 'SELECT value FROM data', stop=lambda: stops.append(True)))
        assert rows == [(i,) for i in range(count)]
        assert stops == [True, True]
    finally:
        db.close()


def test_stop_error_is_not_suppressed():
    db = sqlite3.connect(':memory:')

    def stop():
        raise TimeoutError('stop scan')

    try:
        with pytest.raises(TimeoutError, match='stop scan'):
            list(sqlite_io.streamed_rows(db, 'SELECT 1', stop=stop))
    finally:
        db.close()
