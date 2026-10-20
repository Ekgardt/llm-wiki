"""A valid owner token cannot authorize different task input."""
import sqlite3
from dataclasses import replace

import memory_queue
import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_queue_v3_corruption import _queue


def _leased_query(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue('query', 1, {'query': 'original', 'options': {'mode': 'read'}})
    lease = queue.claim('owner')
    queue.publish_result(lease, operation_id=lease.id, result=b'original result')
    return queue, lease


def _transition(queue, lease, name):
    actions = {
        'heartbeat': lambda: queue.heartbeat(lease),
        'publish': lambda: queue.publish_result(lease, operation_id='different', result=b'changed'),
        'acknowledge': lambda: queue.acknowledge(lease),
        'fail': lambda: queue.fail(lease, memory_queue.QueueFailure('changed')),
    }
    return actions[name]()


@pytest.mark.parametrize('change', [
    {'kind': 'compile'}, {'handler_version': 2}, {'handler_version': True},
    {'input_hash': 'f' * 64}, {'payload': {'query': 'different'}},
])
@pytest.mark.parametrize('action', ['heartbeat', 'publish', 'acknowledge', 'fail'])
def test_protected_transition_refuses_changed_lease_input(tmp_path, change, action):
    queue, lease = _leased_query(tmp_path)
    before = queue.get(lease.id)
    with pytest.raises(memory_queue.LeaseFenceError, match='input'):
        _transition(queue, replace(lease, **change), action)
    assert queue.get(lease.id) == before
    assert queue.acknowledge(lease).state == 'succeeded'


def test_changing_payload_and_its_hash_does_not_change_stored_intent(tmp_path):
    queue, lease = _leased_query(tmp_path)
    payload = {'query': 'different'}
    forged = replace(lease, payload=payload, input_hash=sha256_bytes(canonical_json_bytes(payload)))
    with pytest.raises(memory_queue.LeaseFenceError, match='input'):
        queue.heartbeat(forged)
    assert queue.acknowledge(lease).state == 'succeeded'


def test_nested_mutation_is_not_hidden_by_frozen_lease(tmp_path):
    queue, lease = _leased_query(tmp_path)
    lease.payload['options']['mode'] = 'changed'
    with pytest.raises(memory_queue.LeaseFenceError, match='input'):
        queue.heartbeat(lease)
    assert queue.get(lease.id).state == 'leased'


def test_reordered_mapping_preserves_the_same_input(tmp_path):
    queue, lease = _leased_query(tmp_path)
    reordered = replace(lease, payload=dict(reversed(list(lease.payload.items()))))
    queue.heartbeat(reordered)
    assert queue.acknowledge(reordered).state == 'succeeded'


def test_redacting_a_changed_payload_cannot_make_it_match(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue('query', 1, {'query': 'sk-' + 'a' * 24})
    lease = queue.claim('owner')
    forged = replace(lease, payload={'query': 'sk-' + 'b' * 24})
    assert memory_queue._validated_payload_bytes(forged.payload)[1] == lease.input_hash
    with pytest.raises(memory_queue.LeaseFenceError, match='input'):
        queue.heartbeat(forged)
    assert queue.heartbeat(lease).id == lease.id


def test_stored_payload_corruption_still_demotes_the_task(tmp_path):
    queue, lease = _leased_query(tmp_path)
    with sqlite3.connect(queue.db_path) as database:
        database.execute("UPDATE tasks SET payload_blob=? WHERE id=?", (b'{}', lease.id))
    with pytest.raises(memory_queue.QueueOperationError, match='payload_hash_mismatch'):
        queue.heartbeat(lease)
    assert queue.get(lease.id).state == 'dead'
    assert queue.get(lease.id).error_code == 'payload_hash_mismatch'
