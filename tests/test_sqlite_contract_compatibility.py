"""Compatibility with old imports, persisted pickle names and migration users."""
import ast
import base64
import json
import pickle
from pathlib import Path

import pytest
import reliable_memory
from llm_wiki import sqlite_contracts

BASELINE = json.loads((Path(__file__).parent / 'modules/sqlite_contracts/fixtures/baseline.json').read_text())


@pytest.mark.parametrize('name', ['OperationalDatabaseContract', 'OperationalDatabaseContractError', 'MigrationStatement', 'quote_sqlite_identifier'])
def test_old_import_is_same_object(name):
    assert getattr(reliable_memory, name) is getattr(sqlite_contracts, name)


@pytest.mark.parametrize('record', BASELINE, ids=lambda record: record['type'])
def test_historical_pickle_loads_and_reserializes_identically(record):
    encoded = base64.b64decode(record['pickle'])
    value = pickle.loads(encoded)
    assert type(value) is getattr(sqlite_contracts, record['type'])
    assert repr(value) == record['repr']
    assert pickle.dumps(value, protocol=4) == encoded


@pytest.mark.parametrize('module', ['installed_memory_repair', 'operational_ownership', 'compile_progress_migration', 'markdown_transaction', 'memory_queue'])
def test_production_consumers_do_not_import_value_contracts_from_io(module):
    path = Path(__file__).resolve().parents[1] / 'scripts' / f'{module}.py'
    nodes = _from_imports(path)
    old = [n for n in nodes if n.module == 'reliable_memory']
    names = {a.name for n in old for a in n.names}
    assert not names.intersection({'OperationalDatabaseContract', 'OperationalDatabaseContractError', 'MigrationStatement', 'quote_sqlite_identifier'})


def _from_imports(path):
    return [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ImportFrom)]
