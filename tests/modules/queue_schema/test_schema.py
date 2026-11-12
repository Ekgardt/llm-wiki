"""Pre-extraction identities and real SQLite behavior of the standalone schema."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from llm_wiki import queue_schema

BASELINE = json.loads((Path(__file__).parent / 'fixtures/baseline.json').read_text(encoding='utf-8'))
GROUPS = tuple(BASELINE['counts'])


def fingerprint(definitions):
    return hashlib.sha256(json.dumps(definitions, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize('name', GROUPS)
def test_ordered_definitions_match_baseline(name):
    definitions = getattr(queue_schema, name)
    assert isinstance(definitions, tuple)
    assert len(definitions) == BASELINE['counts'][name]
    assert fingerprint(definitions) == BASELINE['definition_sha256'][name]


@pytest.mark.parametrize('name', GROUPS)
def test_missing_or_modified_definition_is_detected(name):
    definitions = getattr(queue_schema, name)
    expected = BASELINE['definition_sha256'][name]
    assert fingerprint(definitions[:-1]) != expected
    changed = ((definitions[0][0], definitions[0][1] + ' '), *definitions[1:])
    assert fingerprint(changed) != expected


@pytest.fixture
def database():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    for group in GROUPS:
        for _, sql in getattr(queue_schema, group):
            db.execute(sql)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize('group,kind', zip(GROUPS, ('table', 'index', 'trigger')))
def test_sql_creates_all_named_objects(database, group, kind):
    expected = {name for name, _ in getattr(queue_schema, group)}
    actual = {row[0] for row in database.execute(
        "SELECT name FROM sqlite_schema WHERE type=? AND name NOT LIKE 'sqlite_%'", (kind,))}
    assert actual == expected
    assert database.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def insert_task(db, *, state='ready', digest='a' * 64):
    db.execute('''INSERT INTO tasks(id,kind,handler_version,payload_blob,input_hash,
        state,created_at,updated_at,available_at) VALUES ('task','query',1,?,?,?,?,?,?)''',
        (b'{}', digest, state, 'now', 'now', 'now'))


@pytest.mark.parametrize('values', [{'state': 'invented'}, {'digest': 'A' * 64}])
def test_task_contract_rejects_invalid_values(database, values):
    with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
        insert_task(database, **values)


def insert_attempt(db):
    db.execute('''INSERT INTO attempt_history(task_id,attempt,started_at,finished_at,outcome)
        VALUES ('task',1,'now','now','failed')''')


def authorize_ordinary_purge(db):
    db.execute('''INSERT INTO task_purge_authorizations VALUES
        ('task','ordinary','operation',?,'now')''', ('a' * 64,))


def test_history_is_immutable_and_deletion_requires_authorization(database):
    insert_task(database)
    insert_attempt(database)
    with pytest.raises(sqlite3.IntegrityError, match='attempt history is immutable'):
        database.execute("UPDATE attempt_history SET outcome='succeeded'")
    with pytest.raises(sqlite3.IntegrityError, match='attempt history delete is unauthorized'):
        database.execute('DELETE FROM attempt_history')
    authorize_ordinary_purge(database)
    assert database.execute('DELETE FROM attempt_history').rowcount == 1


def test_task_deletion_requires_authorization(database):
    insert_task(database)
    with pytest.raises(sqlite3.IntegrityError, match='task delete is unauthorized'):
        database.execute('DELETE FROM tasks')
    authorize_ordinary_purge(database)
    assert database.execute('DELETE FROM tasks').rowcount == 1


def test_foreign_key_requires_existing_task(database):
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY constraint failed'):
        insert_attempt(database)


def test_fresh_import_needs_no_runtime():
    root = Path(__file__).resolve().parents[3]
    code = '''
import builtins, sys
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name not in {'llm_wiki', 'llm_wiki.queue_schema', '_io'}:
        raise AssertionError('unexpected dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from llm_wiki import queue_schema
assert len(queue_schema.QUEUE_V3_TABLE_SQL) == 22
assert 'memory_queue' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, text=True,
                            capture_output=True, check=False, timeout=30,
                            env={**os.environ, 'PYTHONPATH': str(root / 'scripts')})
    assert result.returncode == 0, result.stderr
