"""Real SQLite checks of planning authority, refusal precedence and reconciliation."""
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from llm_wiki import queue_migration_plan as plan
from llm_wiki.sqlite_contracts import OperationalDatabaseContractError

from tests.slow_machine import LONG_TIMEOUT


def _empty_source(database):
    columns = ', '.join(f'"{name}"' for name in sorted(plan._QUEUE_V2_REQUIRED_TASK_COLUMNS))
    database.execute(f'CREATE TABLE tasks ({columns})')
    database.row_factory = sqlite3.Row
    database.execute('PRAGMA query_only=ON')


def test_planning_reads_only_and_invokes_owner_check_on_supplied_connection():
    with closing(sqlite3.connect(':memory:')) as database:
        _empty_source(database)
        calls = []
        statements, summary = plan._queue_v2_migration_statements(
            database, check_owners=lambda db, tables: calls.append((db, tables)))
        assert calls == [(database, {'tasks'})]
        assert statements == ()
        assert set(summary.values()) == {0}
        assert database.execute('PRAGMA query_only').fetchone()[0] == 1


def _refuse_owner(_database, _tables):
    raise PermissionError('owner remains live')


def test_owner_refusal_propagates_before_reading_task_rows():
    with closing(sqlite3.connect(':memory:')) as database:
        _empty_source(database)
        reads = []
        database.set_trace_callback(reads.append)
        with pytest.raises(PermissionError, match='owner remains live'):
            plan._queue_v2_migration_statements(database, check_owners=_refuse_owner)
        assert 'SELECT * FROM tasks ORDER BY id' not in reads


def test_invalid_schema_refuses_before_owner_callback():
    with closing(sqlite3.connect(':memory:')) as database:
        with pytest.raises(OperationalDatabaseContractError) as caught:
            plan._queue_v2_migration_statements(database, check_owners=_refuse_owner)
        assert caught.value.code == 'queue_v2_schema_incomplete'


def test_each_statement_callback_is_bound_to_its_exact_row():
    with closing(sqlite3.connect(':memory:')) as database:
        database.execute('CREATE TABLE source (id TEXT PRIMARY KEY, payload BLOB)')
        first = plan._queue_v3_insert_statement(name='first', table='source',
            columns=('id', 'payload'), values=('a', b'first'), key_column='id', key='a')
        second = plan._queue_v3_insert_statement(name='second', table='source',
            columns=('id', 'payload'), values=('b', b'second'), key_column='id', key='b')
        database.execute(first.sql, first.parameters)
        database.execute(second.sql, second.parameters)
        assert first.completed(database) and second.completed(database)
        database.execute("UPDATE source SET payload=? WHERE id='a'", (b'changed',))
        assert not first.completed(database)
        assert second.completed(database)


def test_lineage_order_handles_a_chain_longer_than_recursion_limit():
    rows = {str(i): {'id': str(i), 'redrive_of': str(i - 1)} for i in range(1, 2000)}
    rows['0'] = {'id': '0', 'redrive_of': None}
    assert [row['id'] for row in plan._parents_before_children(rows)] == [str(i) for i in range(2000)]


def test_cyclic_lineage_is_refused():
    rows = {'a': {'redrive_of': 'b'}, 'b': {'redrive_of': 'a'}}
    with pytest.raises(OperationalDatabaseContractError) as caught:
        plan._parents_before_children(rows)
    assert caught.value.code == 'queue_v2_lineage_ambiguous'


def test_isolated_import_does_not_load_queue_or_open_database():
    program = '''
import importlib.abc, sqlite3, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'memory_queue', 'markdown_transaction', 'reliable_memory'}, fullname
sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, Guard())
def refuse(*args, **kwargs):
    raise AssertionError('database opened at import')
sqlite3.connect = refuse
import llm_wiki.queue_migration_plan
'''
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, '-c', program, str(root / 'scripts')],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
