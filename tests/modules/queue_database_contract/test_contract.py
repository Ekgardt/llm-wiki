"""Version/header validation preserves the supplied connection lifecycle."""
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest
from llm_wiki import compile_journal_schema as journal
from llm_wiki import queue_database_contract as contract
from llm_wiki.compile_event_records import ProgressCorrupt
from llm_wiki.sqlite_contracts import OperationalDatabaseContractError

ORIGINAL = json.loads((Path(__file__).parents[1] / 'compile_journal_schema/original-contracts.json').read_text(encoding='utf-8'))


def _database(version=3):
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute(f'PRAGMA application_id={contract.QUEUE_V3_CONTRACT.application_id}')
    database.execute(f'PRAGMA user_version={version}')
    if version == journal.QUEUE_SCHEMA_VERSION:
        for statement in journal.SCHEMA_STATEMENTS:
            database.execute(statement)
    return database


def _assert_closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        database.execute('SELECT 1')


def test_original_contract_objects_and_schema_hashes_are_unchanged():
    assert asdict(contract.QUEUE_V3_CONTRACT) == ORIGINAL['queue_contract']
    assert asdict(contract.QUEUE_COMPILE_CONTRACT) == ORIGINAL['compile_contract']
    assert contract.QUEUE_V3_SCHEMA_SHA256 == ORIGINAL['queue_schema_sha256']
    assert contract.QUEUE_COMPILE_SCHEMA_SHA256 == ORIGINAL['compile_schema_sha256']


@pytest.mark.parametrize('version', [3, 4])
def test_supported_connection_and_caller_transaction_are_retained(version):
    with closing(_database(version)) as database:
        database.execute('BEGIN')
        assert contract.checked_queue_connection(database) is database
        assert database.in_transaction
        assert database.execute('SELECT 1').fetchone()[0] == 1


@pytest.mark.parametrize('version', [0, 2, 5])
def test_unsupported_version_closes_connection(version):
    with closing(_database(version)) as database:
        with pytest.raises(OperationalDatabaseContractError, match='unsupported'):
            contract.checked_queue_connection(database)
        _assert_closed(database)


def test_wrong_application_identity_closes_connection():
    with closing(_database()) as database:
        database.execute('PRAGMA application_id=1')
        with pytest.raises(OperationalDatabaseContractError, match='mismatch'):
            contract.checked_queue_connection(database)
        _assert_closed(database)


def test_explicit_expected_version_is_enforced():
    with closing(_database(4)) as database:
        with pytest.raises(OperationalDatabaseContractError, match='mismatch'):
            contract.checked_queue_connection(database, contract.QUEUE_V3_CONTRACT)
        _assert_closed(database)


def test_compile_version_requires_protected_journal_objects():
    with closing(_database(4)) as database:
        database.execute('DROP TRIGGER compile_work_events_no_delete')
        with pytest.raises(ProgressCorrupt):
            contract.checked_queue_connection(database)
        _assert_closed(database)


def test_uninitialized_header_is_never_stamped():
    with closing(sqlite3.connect(':memory:')) as database:
        with pytest.raises(OperationalDatabaseContractError):
            contract.require_supported_queue_contract(database)
        assert database.execute('PRAGMA application_id').fetchone()[0] == 0
        assert database.execute('PRAGMA user_version').fetchone()[0] == 0


def test_base_exception_still_closes_the_supplied_connection(monkeypatch):
    def interrupted(_database):
        raise KeyboardInterrupt('interrupted validation')

    monkeypatch.setattr(contract, 'require_supported_queue_contract', interrupted)
    with closing(_database()) as database:
        with pytest.raises(KeyboardInterrupt, match='interrupted validation'):
            contract.checked_queue_connection(database)
        _assert_closed(database)
