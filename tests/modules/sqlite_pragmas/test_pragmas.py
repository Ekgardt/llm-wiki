"""Real SQLite verifies each connection policy and header refusal."""
import sqlite3
from contextlib import closing

import pytest
from llm_wiki import sqlite_pragmas as policy
from llm_wiki.sqlite_contracts import OperationalDatabaseContract, OperationalDatabaseContractError


def test_writable_connection_gets_required_readback_values(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'policy.sqlite3', isolation_level=None)) as database:
        policy.apply_operational_pragmas(database, 321)
        assert database.row_factory is sqlite3.Row
        assert database.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        values = {name: database.execute(f'PRAGMA {name}').fetchone()[0]
                  for name in ('synchronous', 'foreign_keys', 'trusted_schema', 'busy_timeout')}
        assert values == {'synchronous': 2, 'foreign_keys': 1, 'trusted_schema': 0, 'busy_timeout': 321}


def test_readonly_policy_is_enforced_on_real_readonly_connection(tmp_path):
    path = tmp_path / 'readonly.sqlite3'
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        policy.apply_operational_pragmas(writer, 100)
        writer.execute('CREATE TABLE sample(value TEXT)')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as database:
        policy.apply_readonly_operational_pragmas(database, 123)
        assert database.execute('PRAGMA query_only').fetchone()[0] == 1
        assert database.execute('PRAGMA busy_timeout').fetchone()[0] == 123
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            database.execute("INSERT INTO sample VALUES ('forbidden')")


def test_in_memory_journal_is_not_misreported_as_durable_delete_mode():
    with closing(sqlite3.connect(':memory:')) as database:
        with pytest.raises(sqlite3.OperationalError, match='refused journal_mode'):
            policy.apply_operational_pragmas(database, 10)
        with pytest.raises(OperationalDatabaseContractError, match='journal_mode mismatch'):
            policy.apply_readonly_operational_pragmas(database, 10)


def test_header_initialization_and_subsequent_validation():
    contract = OperationalDatabaseContract(application_id=83, user_version=3)
    with closing(sqlite3.connect(':memory:')) as database:
        policy.validate_or_initialize_operational_contract(database, contract, initialize=True)
        policy.validate_or_initialize_operational_contract(database, contract, initialize=False)
        assert database.execute('PRAGMA application_id').fetchone()[0] == 83
        assert database.execute('PRAGMA user_version').fetchone()[0] == 3


@pytest.mark.parametrize('initialize', [False, True])
def test_conflicting_header_is_refused_without_overwriting_it(initialize):
    contract = OperationalDatabaseContract(application_id=83, user_version=3)
    with closing(sqlite3.connect(':memory:')) as database:
        database.execute('PRAGMA application_id=84')
        with pytest.raises(OperationalDatabaseContractError):
            policy.validate_or_initialize_operational_contract(database, contract, initialize=initialize)
        assert database.execute('PRAGMA application_id').fetchone()[0] == 84


def test_unknown_pragma_does_not_silently_pass_readback():
    with closing(sqlite3.connect(':memory:')) as database:
        with pytest.raises(OperationalDatabaseContractError, match='invalid value'):
            policy.pragma_integer(database, 'nonexistent_policy_pragma')
        with pytest.raises(OperationalDatabaseContractError, match='mismatch'):
            policy.require_pragma(database, 'foreign_keys', 1)
