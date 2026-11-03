"""Real SQLite preserves the original journal schema and refusal behavior."""
import json
import sqlite3
from pathlib import Path

import pytest
from llm_wiki import compile_journal_schema as schema
from llm_wiki.compile_event_records import ProgressCorrupt

ORIGINAL = json.loads((Path(__file__).parent / 'original-contracts.json').read_text(encoding='utf-8'))


@pytest.fixture
def database():
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute('PRAGMA foreign_keys=ON')
    database.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY)')
    database.execute("INSERT INTO tasks VALUES ('task')")
    for statement in schema.SCHEMA_STATEMENTS:
        database.execute(statement)
    yield database
    database.close()


def _insert(database, sequence=0, task='task'):
    database.execute('INSERT INTO compile_work_events VALUES (?,?,?,?,?,?,?,?)',
                     ('work', sequence, task, 'source', 'pending', 'ready', '{}', 'digest'))


def test_sql_and_version_match_original_bytes(database):
    assert list(schema.SCHEMA_STATEMENTS) == ORIGINAL['journal_sql']
    assert schema.QUEUE_SCHEMA_VERSION == ORIGINAL['journal_version']
    schema._require_schema(database)


@pytest.mark.parametrize('sql', [
    'DROP TRIGGER compile_work_events_no_update',
    'DROP TRIGGER compile_work_events_no_delete',
    'ALTER TABLE compile_work_events ADD COLUMN unexpected TEXT',
])
def test_missing_or_changed_object_is_refused_without_closing_caller(database, sql):
    database.execute(sql)
    with pytest.raises(ProgressCorrupt, match='schema or protection changed'):
        schema._require_schema(database)
    assert database.execute('SELECT 1').fetchone()[0] == 1


def test_same_trigger_name_with_different_sql_is_refused(database):
    database.execute('DROP TRIGGER compile_work_events_no_update')
    database.execute('CREATE TRIGGER compile_work_events_no_update BEFORE UPDATE ON compile_work_events BEGIN SELECT 1; END')
    with pytest.raises(ProgressCorrupt):
        schema._require_schema(database)


@pytest.mark.parametrize('sql', [
    "UPDATE compile_work_events SET stage='published'", 'DELETE FROM compile_work_events',
])
def test_protected_rows_cannot_be_updated_or_deleted(database, sql):
    _insert(database)
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(sql)
    assert database.execute('SELECT COUNT(*) FROM compile_work_events').fetchone()[0] == 1


def test_sequence_and_foreign_key_constraints_remain(database):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(database, sequence=-1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(database, task='absent')
    _insert(database)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(database)
