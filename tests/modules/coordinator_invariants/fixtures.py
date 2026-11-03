"""Actual coordinator DDL and small valid fenced rows for corruption tests."""
import sqlite3
from contextlib import closing

import pytest
from llm_wiki.coordinator_schema import _COORDINATOR_V3_TABLE_SQL


@pytest.fixture
def database():
    with closing(sqlite3.connect(':memory:')) as connection:
        for _name, statement in _COORDINATOR_V3_TABLE_SQL:
            connection.execute(statement)
        yield connection


def insert(connection, table, values):
    columns = ','.join(values)
    placeholders = ','.join('?' for _ in values)
    connection.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values()))


def seed_owner(connection, role):
    insert(connection, 'maintenance_owner_epochs', {'role': role, 'scope': 'scope', 'last_epoch': 1})
    insert(connection, 'maintenance_owners', {
        'role': role, 'scope': 'scope', 'actor_id': 'actor', 'owner_token': 'token',
        'process_id': 42, 'process_start_identity': 'process', 'fencing_epoch': 1,
        'acquired_at': 'now', 'heartbeat_at': 'now', 'expires_at': 'later',
    })


def projection_values(table, role):
    common = {'canonical_role': role, 'canonical_scope': 'scope', 'process_id': 42,
              'process_start_identity': 'process', 'heartbeat_at': 'now', 'expires_at': 'later', 'fencing_epoch': 1}
    if table == 'intent_fences':
        return common | {'intent_id': 'a' * 64, 'mode': 'capture', 'token': 'fence', 'canonical_actor_id': 'actor',
                         'canonical_owner_token': 'token', 'canonical_fencing_epoch': 1}
    extra = {'writer_owners': {'gate_name': 'gate', 'owner_token': 'token', 'thread_id': 1, 'acquired_at': 'now'},
             'project_leases': {'project': 'project', 'lease_token': 'token', 'owner': 'agent'}}
    return common | {'actor_id': 'actor'} | extra[table]


def seed_projection(connection, table):
    role = {'writer_owners': 'markdown-writer', 'project_leases': 'project', 'intent_fences': 'capture'}[table]
    seed_owner(connection, role)
    if table == 'intent_fences':
        insert(connection, 'intent_fence_epochs', {'intent_id': 'a' * 64, 'last_epoch': 1})
    insert(connection, table, projection_values(table, role))


def seed_binding(connection):
    insert(connection, 'capture_binding_projections', {
        'intent_id': 'a' * 64, 'task_id': 'task', 'active_link_digest': 'b' * 64,
        'seal_digest': 'c' * 64, 'projected_at': 'now', 'intent_fence_token': 'fence', 'intent_fence_epoch': 1,
    })


def seed_blackboard(connection):
    for resource in ('one', 'two'):
        insert(connection, 'blackboard_claim_epochs', {'project': 'project', 'resource': resource, 'last_epoch': 1})
        insert(connection, 'blackboard_claims', {
            'project': 'project', 'resource': resource, 'claim_id': 'a' * 64, 'agent': 'agent',
            'lease_token': 'b' * 64, 'fencing_epoch': 1, 'heartbeat_at': 'now', 'expires_at': 'later',
        })


def seed_operation(connection, position):
    insert(connection, 'transaction', {'id': 'transaction', 'operation_id': 'operation', 'request_hash': 'hash',
                                     'state': 'prepared', 'preconditions_json': '{}', 'plan_hash': 'plan', 'created_at': 'now', 'updated_at': 'now'})
    insert(connection, 'operation', {'transaction_id': 'transaction', 'position': position, 'kind': 'create', 'path': 'note.md',
                                   'before_hash': 'before', 'after_hash': 'after', 'parent_device': 1, 'parent_inode': 2, 'applied': 0})
