"""Actual SQLite rows distinguish valid fences from drift without writing state."""
import sqlite3

import pytest
from llm_wiki import coordinator_invariants as invariants

from tests.modules.coordinator_invariants.fixtures import database as database
from tests.modules.coordinator_invariants.fixtures import (
    seed_binding,
    seed_blackboard,
    seed_operation,
    seed_owner,
    seed_projection,
)


@pytest.mark.parametrize('table', ['writer_owners', 'project_leases', 'intent_fences'])
def test_valid_projection_and_row_counts(database, table):
    seed_projection(database, table)
    assert invariants._coordinator_v3_cross_table_invariant(database)
    invariants._require_v3_invariants(database)
    counts = invariants._coordinator_v3_row_counts(database)
    assert counts['maintenance_owners'] == counts[table] == 1
    assert counts['operation'] == 0


@pytest.mark.parametrize('table', ['writer_owners', 'project_leases', 'intent_fences'])
@pytest.mark.parametrize(('column', 'value'), [('process_id', 43), ('process_start_identity', 'changed')])
def test_projection_process_mismatch_refuses(database, table, column, value):
    seed_projection(database, table)
    database.execute(f'UPDATE "{table}" SET {column}=?', (value,))
    assert not invariants._coordinator_v3_base_cross_table_invariant(database)
    with pytest.raises(sqlite3.DatabaseError, match='database invariant failed') as raised:
        invariants._require_v3_invariants(database)
    assert raised.value.code == 'coordinator_v3_validation_failed'


@pytest.mark.parametrize('sql', ['UPDATE maintenance_owner_epochs SET last_epoch=0', 'DELETE FROM maintenance_owner_epochs'])
def test_owner_requires_adequate_epoch(database, sql):
    seed_owner(database, 'capture')
    assert invariants._coordinator_v3_base_cross_table_invariant(database)
    database.execute(sql)
    assert not invariants._coordinator_v3_base_cross_table_invariant(database)


@pytest.mark.parametrize('sql', ['UPDATE intent_fence_epochs SET last_epoch=0', 'DELETE FROM intent_fence_epochs'])
def test_intent_requires_adequate_epoch(database, sql):
    seed_projection(database, 'intent_fences')
    assert invariants._coordinator_v3_base_cross_table_invariant(database)
    database.execute(sql)
    assert not invariants._coordinator_v3_base_cross_table_invariant(database)


def test_capture_binding_requires_its_exact_fence(database):
    seed_binding(database)
    assert not invariants._coordinator_v3_base_cross_table_invariant(database)
    seed_projection(database, 'intent_fences')
    assert invariants._coordinator_v3_base_cross_table_invariant(database)
    database.execute("UPDATE capture_binding_projections SET intent_fence_token='other'")
    assert not invariants._coordinator_v3_base_cross_table_invariant(database)


@pytest.mark.parametrize(('column', 'value'), [('agent', 'other'), ('lease_token', 'c' * 64), ('heartbeat_at', 'changed'), ('expires_at', 'changed')])
def test_blackboard_claim_set_is_coherent(database, column, value):
    seed_blackboard(database)
    assert invariants._coordinator_v3_blackboard_invariant(database)
    database.execute(f"UPDATE blackboard_claims SET {column}=? WHERE resource='two'", (value,))
    assert not invariants._coordinator_v3_blackboard_invariant(database)
    with pytest.raises(sqlite3.DatabaseError, match='database invariant failed'):
        invariants._require_v3_invariants(database)


def test_blackboard_requires_exact_epoch(database):
    seed_blackboard(database)
    database.execute("UPDATE blackboard_claim_epochs SET last_epoch=2 WHERE resource='two'")
    assert not invariants._coordinator_v3_blackboard_invariant(database)


def test_negative_operation_position_is_not_hidden_by_sqlite_integrity(database):
    seed_operation(database, -1)
    invariants._require_v3_integrity(database)
    assert invariants._v3_operation_violations(database) == 1
    with pytest.raises(sqlite3.DatabaseError, match='database invariant failed'):
        invariants._require_v3_invariants(database)


def readonly_authorizer(action, first, second, database, trigger):
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if second is None else sqlite3.SQLITE_DENY
    allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def test_validation_preserves_callers_transaction_and_refuses_to_mutate(database):
    seed_projection(database, 'intent_fences')
    seed_binding(database)
    seed_blackboard(database)
    seed_operation(database, 0)
    assert database.in_transaction
    changes = database.total_changes
    database.set_authorizer(readonly_authorizer)
    invariants._require_v3_invariants(database)
    assert invariants._coordinator_v3_row_counts(database)['operation'] == 1
    assert database.in_transaction
    assert database.total_changes == changes
    database.set_authorizer(None)
    database.rollback()
    assert database.execute('SELECT COUNT(*) FROM maintenance_owners').fetchone()[0] == 0
