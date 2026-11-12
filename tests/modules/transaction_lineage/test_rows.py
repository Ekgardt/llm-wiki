"""Actual SQLite rows establish lineage without mutating the caller's database."""
import sqlite3
from contextlib import closing

import pytest
import transaction_lineage as lineage

from tests.modules.transaction_lineage.fixtures import _add, _database


@pytest.fixture
def database():
    with closing(_database()) as connection:
        yield connection


def test_parent_cycles_terminate_and_preserve_the_callers_transaction(database):
    _add(database, 'q', 'request-q', 'quarantined', parent='middle')
    _add(database, 'middle', 'request-middle', 'prepared', parent='q')
    _add(database, 'winner', 'request-winner', 'committed', parent='middle')
    _add(database, 'unrelated', 'other', 'committed')
    database.execute('PRAGMA query_only = ON')
    assert database.execute('PRAGMA query_only').fetchone() == (1,)
    changes = database.total_changes
    assert database.in_transaction
    assert lineage.chain_resolved_ids(database) == {'q', 'middle'}
    assert lineage.resolved_quarantines(database) == frozenset({'q'})
    assert lineage.quarantine_witnesses(database) == frozenset({'q', 'middle', 'winner'})
    assert database.total_changes == changes
    assert database.in_transaction


def test_only_the_same_request_retry_resolves_a_replacement(database):
    operations = (('replace', 'knowledge/daily/day.md'),)
    _add(database, 'q', 'request', 'quarantined', operations)
    _add(database, 'other-q', 'different-request', 'quarantined', operations)
    _add(database, 'winner', 'request:cas:2', 'committed', operations)
    assert lineage.ordinal_resolved_ids(database) == {'q'}
    assert lineage.resolved_quarantines(database) == frozenset({'q'})
    assert lineage.committed_created_paths(database) == set()


def test_create_outcomes_require_every_intended_path_and_no_replace_substitution(database):
    _add(database, 'q', 'request', 'quarantined', (('create', 'a.md'), ('create', 'b.md')))
    _add(database, 'replace-q', 'replace-request', 'quarantined', (('replace', 'a.md'),))
    _add(database, 'a', 'create-a', 'committed', (('create', 'a.md'),))
    assert lineage.resolved_quarantines(database) == frozenset()
    _add(database, 'b', 'create-b', 'committed', (('create', 'b.md'),))
    assert lineage.resolved_quarantines(database) == frozenset({'q'})
    assert lineage.quarantine_witnesses(database) == frozenset({'q', 'replace-q', 'a', 'b'})


def test_missing_schema_propagates_instead_of_claiming_no_unresolved_work():
    with closing(sqlite3.connect(':memory:')) as connection:
        with pytest.raises(sqlite3.OperationalError):
            lineage.resolved_quarantines(connection)
