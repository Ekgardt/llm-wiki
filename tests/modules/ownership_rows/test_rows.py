"""Actual fenced SQL behavior, first exercised against the original helpers."""
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from llm_wiki import ownership_rows as rows
from llm_wiki.coordinator_schema import COORDINATOR_V3_TABLE_SQL
from llm_wiki.ownership_contracts import OperationalOwnershipError, ProcessIdentity

from tests.modules.coordinator_invariants.fixtures import seed_binding, seed_projection

NOW = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.fixture
def database():
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        for _name, sql in COORDINATOR_V3_TABLE_SQL:
            connection.execute(sql)
        yield connection


def _request():
    return rows.AcquireRequest('doctor', 'scope', 'actor', 'token',
                                ProcessIdentity(42, 'process'), None, None, None)


def _count(database, table):
    return database.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def _owner(database):
    return database.execute('SELECT * FROM maintenance_owners').fetchone()


def _seed(database, table):
    seed_projection(database, table)
    if table == 'intent_fences':
        seed_binding(database)


def test_epoch_is_scoped_by_role_and_scope(database):
    request = _request()
    assert rows.next_owner_epoch(database, request) == 1
    assert rows.next_owner_epoch(database, request) == 2
    assert rows.next_owner_epoch(database, replace(request, scope='other')) == 1
    assert rows.next_owner_epoch(database, replace(request, role='capture')) == 1
    database.rollback()
    assert _count(database, 'maintenance_owner_epochs') == 0


def test_insert_preserves_process_times_and_conflict_error(database):
    request = _request()
    rows.insert_owner_row(database, request, 7, NOW, NOW + timedelta(seconds=30))
    row = _owner(database)
    assert rows.row_process(row) == request.process
    assert rows.owner_key(row) == ('doctor', 'scope', 'actor', 'token', 7)
    assert (row['acquired_at'], row['heartbeat_at'], row['expires_at']) == (
        '2026-11-03T00:00:00.000000Z', '2026-11-03T00:00:00.000000Z',
        '2026-11-03T00:00:30.000000Z',
    )
    with pytest.raises(OperationalOwnershipError) as error:
        rows.insert_owner_row(database, replace(request, scope='other'), 1, NOW, NOW)
    assert error.value.code == 'owner_identity_conflict'
    assert _count(database, 'maintenance_owners') == 1


@pytest.mark.parametrize('table', ['writer_owners', 'project_leases', 'intent_fences'])
def test_projection_cleanup_keeps_owner_and_caller_rollback(database, table):
    _seed(database, table)
    database.commit()
    owner = _owner(database)
    database.execute('BEGIN')
    rows.delete_owner_projections(database, owner)
    assert _count(database, table) == 0
    assert _count(database, 'capture_binding_projections') == 0
    assert _count(database, 'maintenance_owners') == 1
    database.rollback()
    assert _count(database, table) == 1


@pytest.mark.parametrize('table', ['writer_owners', 'project_leases', 'intent_fences'])
@pytest.mark.parametrize('field,value', [
    ('role', 'doctor'), ('scope', 'other'), ('actor_id', 'other'),
    ('owner_token', 'other'), ('fencing_epoch', 2),
])
def test_every_fencing_component_protects_other_projections(database, table, field, value):
    _seed(database, table)
    stale = dict(_owner(database)) | {field: value}
    bindings = _count(database, 'capture_binding_projections')
    rows.delete_owner_projections(database, stale)
    assert _count(database, table) == 1
    assert _count(database, 'capture_binding_projections') == bindings


def test_binding_is_deleted_only_for_the_exact_intent_fence(database):
    _seed(database, 'intent_fences')
    database.execute("UPDATE capture_binding_projections SET intent_fence_token='another'")
    rows.delete_owner_projections(database, _owner(database))
    assert _count(database, 'intent_fences') == 0
    assert _count(database, 'capture_binding_projections') == 1


def _deny_transaction_control(action, _argument, _column, _database, _trigger):
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def test_helpers_leave_transaction_control_to_caller(database):
    request = _request()
    database.execute('BEGIN')
    database.set_authorizer(_deny_transaction_control)
    epoch = rows.next_owner_epoch(database, request)
    rows.insert_owner_row(database, request, epoch, NOW, NOW)
    rows.delete_owner_projections(database, _owner(database))
    assert database.in_transaction
    database.set_authorizer(None)
    database.rollback()
    assert _count(database, 'maintenance_owners') == 0
    assert _count(database, 'maintenance_owner_epochs') == 0


def test_absent_returned_epoch_is_refused(database):
    database.execute('CREATE TRIGGER no_epoch BEFORE INSERT ON maintenance_owner_epochs '
                     'BEGIN SELECT RAISE(IGNORE); END')
    with pytest.raises(OperationalOwnershipError) as error:
        rows.next_owner_epoch(database, _request())
    assert error.value.code == 'owner_epoch_unavailable'


def test_ignored_owner_insert_is_refused(database):
    database.execute('CREATE TRIGGER no_owner BEFORE INSERT ON maintenance_owners '
                     'BEGIN SELECT RAISE(IGNORE); END')
    with pytest.raises(OperationalOwnershipError) as error:
        rows.insert_owner_row(database, _request(), 1, NOW, NOW)
    assert error.value.code == 'owner_fence_lost'
