"""Statement-level rollback, restart and final refusal without filesystem helpers."""
import sqlite3
from contextlib import closing

import pytest
from llm_wiki.sqlite_contracts import MigrationStatement, OperationalDatabaseContractError
from llm_wiki.sqlite_migrations import run_resumable_migration


def _completed(database):
    return database.execute("SELECT 1 FROM sqlite_schema WHERE name='sample'").fetchone() is not None


def _statement():
    return MigrationStatement('create_sample', 'CREATE TABLE sample(value TEXT)', _completed)


@pytest.mark.parametrize('phase, committed', [('before', False), ('after_execute', False), ('after_commit', True)])
def test_failure_at_each_boundary_preserves_exact_error_and_resumes(phase, committed):
    error = KeyboardInterrupt('interrupted caller')

    def interrupt(event):
        if event == f'{phase}:create_sample':
            raise error

    with closing(sqlite3.connect(':memory:', isolation_level=None)) as database:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_resumable_migration(database, (_statement(),), final_invariant=_completed, killpoint=interrupt)
        assert caught.value is error
        assert _completed(database) is committed
        assert not database.in_transaction
        events = []
        run_resumable_migration(database, (_statement(),), final_invariant=_completed, killpoint=events.append)
        expected = [] if committed else ['before:create_sample', 'after_execute:create_sample', 'after_commit:create_sample']
        assert events == expected
        assert _completed(database)


def test_statement_invariant_failure_rolls_back_ddl():
    statement = MigrationStatement('create_sample', 'CREATE TABLE sample(value TEXT)', lambda db: False)
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as database:
        with pytest.raises(OperationalDatabaseContractError) as caught:
            run_resumable_migration(database, (statement,), final_invariant=lambda db: True)
        assert caught.value.code == 'operational_migration_incomplete'
        assert not _completed(database)


def test_final_invariant_failure_does_not_undo_previously_committed_statement():
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as database:
        with pytest.raises(OperationalDatabaseContractError) as caught:
            run_resumable_migration(database, (_statement(),), final_invariant=lambda db: False)
        assert caught.value.code == 'operational_migration_incomplete'
        assert _completed(database)


def test_duplicate_names_refuse_before_any_sql():
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as database:
        sql = []
        database.set_trace_callback(sql.append)
        with pytest.raises(ValueError, match='unique'):
            run_resumable_migration(database, (_statement(), _statement()), final_invariant=_completed)
        assert sql == []
