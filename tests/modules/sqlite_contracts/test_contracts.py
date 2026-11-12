"""Operational value contracts preserve validation, immutability and quoting."""
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from llm_wiki.sqlite_contracts import (
    MigrationStatement,
    OperationalDatabaseContract,
    OperationalDatabaseContractError,
    quote_sqlite_identifier,
)


@pytest.mark.parametrize('value', [True, False, 0, -1, '3', None, 1.5])
@pytest.mark.parametrize('field', ['application_id', 'user_version'])
def test_contract_rejects_nonpositive_or_noninteger_identity(value, field):
    args = {'application_id': 123, 'user_version': 3, field: value}
    with pytest.raises(ValueError, match=f'{field} must be a positive integer'):
        OperationalDatabaseContract(**args)


def test_contract_keeps_default_version_and_is_frozen():
    value = OperationalDatabaseContract(123)
    assert value.user_version == 3
    with pytest.raises(FrozenInstanceError):
        value.user_version = 4


@pytest.mark.parametrize('name,sql', [('', 'SELECT 1'), (None, 'SELECT 1'), ('n', ''), ('n', '  '), ('n', None)])
def test_migration_requires_nonempty_name_and_sql(name, sql):
    with pytest.raises(ValueError):
        MigrationStatement(name, sql, bool)


@pytest.mark.parametrize('completed', [None, False, 3])
def test_migration_requires_callback(completed):
    with pytest.raises(TypeError, match='completed invariant must be callable'):
        MigrationStatement('n', 'SELECT 1', completed)


def test_callback_is_not_part_of_value_equality_or_repr():
    first = MigrationStatement('n', 'SELECT ?', bool, (1,))
    second = MigrationStatement('n', 'SELECT ?', str, (1,))
    assert first == second
    assert repr(first) == "MigrationStatement(name='n', sql='SELECT ?', parameters=(1,))"
    assert first.completed is bool
    with pytest.raises(FrozenInstanceError):
        first.sql = 'DROP TABLE tasks'


@pytest.mark.parametrize('name,expected', [('simple', '"simple"'), ('a"b', '"a""b"'), ('', '""'), ('таблица', '"таблица"'), ('x;DROP TABLE y', '"x;DROP TABLE y"')])
def test_identifier_quoting_is_exact(name, expected):
    assert quote_sqlite_identifier(name) == expected


def test_error_retains_code_and_database_error_ancestry():
    import sqlite3
    error = OperationalDatabaseContractError('bad')
    assert isinstance(error, sqlite3.DatabaseError)
    assert error.code == 'operational_database_contract_mismatch'
    assert str(error) == 'bad'


def test_import_does_not_load_io_or_open_database():
    program = '''
import importlib.abc, sqlite3, sys
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'reliable_memory','memory_queue','markdown_transaction'}
def deny_connect(*args, **kwargs):
    raise AssertionError('database opened')
sqlite3.connect = deny_connect
sys.meta_path.insert(0, Deny())
from llm_wiki.sqlite_contracts import OperationalDatabaseContract, MigrationStatement
assert OperationalDatabaseContract(1).user_version == 3
assert MigrationStatement('n', 'SELECT 1', bool).name == 'n'
'''
    scripts = str(Path(__file__).resolve().parents[3] / 'scripts')
    result = subprocess.run([sys.executable, '-c', program, scripts], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_named_migration_error_is_the_same_database_error_type():
    from llm_wiki.sqlite_contracts import migration_error
    error = migration_error('specific_code', 'specific detail')
    assert type(error) is OperationalDatabaseContractError
    assert error.code == 'specific_code'
    assert str(error) == 'specific detail'
