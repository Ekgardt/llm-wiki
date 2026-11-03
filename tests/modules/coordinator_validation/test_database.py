"""Real database qualification, exercised before and after exact transfer."""
import sqlite3
import time
from contextlib import closing

import pytest
from llm_wiki import coordinator_validation as validation
from llm_wiki.coordinator_schema import _COORDINATOR_V3_CONTRACT, _COORDINATOR_V3_TABLE_SQL
from llm_wiki.sqlite_contracts import OperationalDatabaseContractError
from llm_wiki.sqlite_io import open_operational_db


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / 'coordinator.sqlite3'
    with closing(open_operational_db(path, busy_ms=0,
                                    contract=_COORDINATOR_V3_CONTRACT,
                                    initialize_contract=True)) as database:
        for _name, sql in _COORDINATOR_V3_TABLE_SQL:
            database.execute(sql)
    return path


def test_clean_database_certification_preserves_bytes(database_path):
    before = database_path.read_bytes()
    report = validation.validate_coordinator_v3_database(
        database_path, state_root=database_path.parent)
    assert report['application_id'] == _COORDINATOR_V3_CONTRACT.application_id
    assert report['user_version'] == _COORDINATOR_V3_CONTRACT.user_version
    assert report['integrity_check'] == 'ok'
    assert report['foreign_key_check'] == []
    assert report['journal_mode'] == 'delete'
    assert report['synchronous'] == 2
    assert report['trusted_schema'] == 0
    assert report['row_counts'] == {name: 0 for name, _sql in _COORDINATOR_V3_TABLE_SQL}
    assert database_path.read_bytes() == before


def test_containment_refuses_a_real_database_outside_root(database_path, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(PermissionError, match='outside the state root'):
        validation.validate_coordinator_v3_database(database_path, state_root=other)


def test_missing_database_is_refused_without_creation(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(PermissionError, match='outside the state root'):
        validation.validate_coordinator_v3_database(path, state_root=tmp_path)
    assert not path.exists()


@pytest.mark.parametrize('check', ['require_coordinator_v3_openable', 'validate_coordinator_v3_database'])
def test_missing_table_is_refused_by_both_checks(database_path, check):
    with closing(sqlite3.connect(database_path)) as database:
        database.execute('DROP TABLE operation')
        database.commit()
    with pytest.raises(OperationalDatabaseContractError, match='schema is incomplete'):
        getattr(validation, check)(database_path, state_root=database_path.parent)


@pytest.mark.parametrize('pragma', ['application_id', 'user_version'])
def test_wrong_database_identity_is_refused(database_path, pragma):
    with closing(sqlite3.connect(database_path)) as database:
        database.execute(f'PRAGMA {pragma}=0')
    with pytest.raises(OperationalDatabaseContractError):
        validation.validate_coordinator_v3_database(database_path, state_root=database_path.parent)


def _no_history(action, argument, _column, _database, _trigger):
    forbidden = {sqlite3.SQLITE_PRAGMA: {'integrity_check', 'foreign_key_check'},
                 sqlite3.SQLITE_READ: {'operation', 'transaction'}}
    if argument in forbidden.get(action, set()):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def test_admission_skips_history_but_certification_still_reads_it(database_path, monkeypatch):
    opened = []
    original = validation.open_readonly_operational_db

    def observed(*args, **kwargs):
        database = original(*args, **kwargs)
        database.set_authorizer(_no_history)
        opened.append(database)
        return database

    monkeypatch.setattr(validation, 'open_readonly_operational_db', observed)
    validation.require_coordinator_v3_openable(database_path, state_root=database_path.parent)
    with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
        validation.validate_coordinator_v3_database(database_path, state_root=database_path.parent)
    assert len(opened) == 2
    for database in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            database.execute('SELECT 1')


def test_admission_honors_expired_deadline(database_path):
    with pytest.raises(TimeoutError):
        validation.require_coordinator_v3_openable(database_path, state_root=database_path.parent,
                                                 deadline=time.monotonic() - 1)
