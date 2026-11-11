"""Real SQLite repair/refusal behavior without the queue executor."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from llm_wiki import queue_schema_operations as schema
from llm_wiki.sqlite_contracts import OperationalDatabaseContractError


@pytest.fixture
def database():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def test_creation_callbacks_and_trigger_phase_are_exact(database):
    statements = schema.queue_v3_statements(include_triggers=False)
    assert len(statements) == 27
    assert not schema.queue_v3_schema_complete(database, include_triggers=False)
    for statement in statements:
        assert not statement.completed(database)
        database.execute(statement.sql, statement.parameters)
        assert statement.completed(database)
    assert schema.queue_v3_schema_complete(database, include_triggers=False)
    assert not schema.queue_v3_schema_complete(database)
    _create_triggers(database)
    assert schema.queue_v3_schema_complete(database)


def _create_triggers(database):
    statements = schema.queue_v3_trigger_statements()
    assert len(statements) == 35
    for statement in statements:
        database.execute(statement.sql, statement.parameters)
        assert statement.completed(database)


def test_populated_malformed_table_is_not_rebuilt_without_permission(database):
    database.execute('CREATE TABLE tasks (retained TEXT)')
    database.execute("INSERT INTO tasks VALUES ('evidence')")
    with pytest.raises(OperationalDatabaseContractError) as caught:
        schema.repair_partial_queue_v3_schema(database, allow_populated_rebuild=False)
    assert caught.value.code == 'queue_v3_schema_conflict'
    assert database.execute('SELECT retained FROM tasks').fetchone()[0] == 'evidence'


def test_empty_malformed_table_can_be_removed_for_later_recreation(database):
    database.execute('CREATE TABLE tasks (wrong TEXT)')
    schema.repair_partial_queue_v3_schema(database, allow_populated_rebuild=False)
    assert database.execute("SELECT name FROM sqlite_schema WHERE name='tasks'").fetchall() == []


def test_explicit_rebuild_permission_retains_previous_behavior(database):
    database.execute('CREATE TABLE tasks (wrong TEXT)')
    database.execute("INSERT INTO tasks VALUES ('source will rebuild this')")
    schema.repair_partial_queue_v3_schema(database, allow_populated_rebuild=True)
    assert database.execute("SELECT name FROM sqlite_schema WHERE name='tasks'").fetchall() == []


def test_unexpected_object_is_refused_without_permission(database):
    database.execute('CREATE TABLE unexpected (value TEXT)')
    with pytest.raises(OperationalDatabaseContractError, match='unexpected schema objects'):
        schema.repair_partial_queue_v3_schema(database, allow_populated_rebuild=False)
    assert database.execute("SELECT name FROM sqlite_schema WHERE name='unexpected'").fetchone()


def test_unsupported_object_kind_is_not_dropped_even_with_permission(database):
    database.execute('CREATE VIEW unexpected AS SELECT 1')
    with pytest.raises(OperationalDatabaseContractError, match='unsupported schema object'):
        schema.repair_partial_queue_v3_schema(database, allow_populated_rebuild=True)
    assert database.execute("SELECT type FROM sqlite_schema WHERE name='unexpected'").fetchone()[0] == 'view'


def test_unreadable_table_is_treated_as_populated(database):
    assert schema.table_is_populated(database, 'does_not_exist')


def test_drop_quotes_exact_identifier(database):
    name = 'safe" --'
    database.execute('CREATE TABLE safe (value TEXT)')
    database.execute('CREATE TABLE "safe"" --" (value TEXT)')
    schema.drop_schema_object(database, 'table', name)
    assert database.execute("SELECT name FROM sqlite_schema WHERE name='safe'").fetchone()
    assert database.execute('SELECT name FROM sqlite_schema WHERE name=?', (name,)).fetchone() is None


class _FailAfterDrop:
    def __init__(self, database):
        self.database = database

    def execute(self, sql, *parameters):
        result = self.database.execute(sql, *parameters)
        if sql.startswith('DROP'):
            raise sqlite3.OperationalError('injected after drop')
        return result

    def commit(self):
        self.database.commit()

    def rollback(self):
        self.database.rollback()


def test_failed_drop_rolls_back_the_actual_ddl(database):
    database.execute('CREATE TABLE retained (value TEXT)')
    with pytest.raises(sqlite3.OperationalError, match='injected after drop'):
        schema.drop_schema_object(_FailAfterDrop(database), 'table', 'retained')
    assert database.execute("SELECT name FROM sqlite_schema WHERE name='retained'").fetchone()
    assert not database.in_transaction


def test_drop_order_keeps_triggers_before_indexes_and_tables():
    objects = [('table', 'a'), ('index', 'b'), ('trigger', 'c')]
    assert sorted(objects, key=schema.drop_order_key) == list(reversed(objects))


def test_owner_import_does_not_need_executor_or_open_a_database():
    program = '''
import importlib.abc, sqlite3, sys
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'memory_queue','markdown_transaction','compile_memory','reliable_memory'}
def refuse(*args, **kwargs):
    raise AssertionError('database opened during import')
sqlite3.connect = refuse
sys.meta_path.insert(0, Deny())
from llm_wiki.queue_schema_operations import queue_v3_statements
assert len(queue_v3_statements()) == 62
'''
    result = subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve().parents[3] / 'scripts')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
