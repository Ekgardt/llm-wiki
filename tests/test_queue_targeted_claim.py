"""Foreground selection must preserve every ordinary queue admission fence."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import memory_queue
import operational_ownership
import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.slow_machine import LONG_TIMEOUT
from tests.test_queue_v3_capture_links import _capture_binding, _coordinator
from tests.test_queue_v3_corruption import _queue


def _pair(tmp_path):
    queue = _queue(tmp_path)
    target = queue.enqueue('query', 1, {'query': 'requested'})
    unrelated = queue.enqueue('query', 1, {'query': 'unrelated'}, priority=100)
    return queue, target, unrelated


def test_target_claim_does_not_take_higher_priority_unrelated_work(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    lease = queue.claim('foreground', task_id=target)
    assert lease.id == target
    assert lease.payload == {'query': 'requested'}
    assert queue.get(unrelated).state == 'ready'
    assert queue.get(unrelated).attempts == 0
    assert queue.claim('another-foreground', task_id=target) is None
    assert queue.claim('ordinary-worker').id == unrelated


def test_ordinary_claim_keeps_existing_priority_order(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    assert queue.claim('ordinary-worker').id == unrelated
    assert queue.claim('foreground', task_id=target).id == target


@pytest.mark.parametrize('task_id', ['absent', "x' OR 1=1 --"])
def test_missing_target_never_falls_through_to_unrelated_work(tmp_path, task_id):
    queue, target, unrelated = _pair(tmp_path)
    assert queue.claim('foreground', task_id=task_id) is None
    assert [queue.get(item).attempts for item in (target, unrelated)] == [0, 0]


@pytest.mark.parametrize('task_id', ['', 1, True, 'x' * 257])
def test_invalid_task_id_is_refused_without_claiming(tmp_path, task_id):
    queue, target, unrelated = _pair(tmp_path)
    with pytest.raises(ValueError, match='task ID'):
        queue.claim('foreground', task_id=task_id)
    assert [queue.get(item).attempts for item in (target, unrelated)] == [0, 0]


def _finish(queue, lease):
    queue.publish_result(lease, operation_id=lease.id, result=b'done')
    queue.acknowledge(lease)


def _make_ineligible(queue, lease, state):
    actions = {
        'leased': lambda: None,
        'blocked': lambda: queue.fail(lease, memory_queue.QueueFailure(
            'unknown', blocked_capability='provider_outcome_unknown')),
        'dead': lambda: queue.fail(lease, memory_queue.QueueFailure('failed', permanent=True)),
        'cancelled': lambda: queue.cancel(lease.id),
        'succeeded': lambda: _finish(queue, lease),
    }
    actions[state]()


@pytest.mark.parametrize('state', ['leased', 'blocked', 'dead', 'cancelled', 'succeeded'])
def test_target_selection_cannot_override_task_state(tmp_path, state):
    queue = _queue(tmp_path)
    target = queue.enqueue('query', 1, {})
    lease = queue.claim('original')
    _make_ineligible(queue, lease, state)
    unrelated = queue.enqueue('query', 1, {'unrelated': True})
    before = queue.get(target)
    assert queue.claim('foreground', task_id=target) is None
    assert queue.get(target) == before
    assert queue.get(unrelated).attempts == 0


def test_target_selection_keeps_future_availability(tmp_path):
    queue = _queue(tmp_path)
    target = queue.enqueue('query', 1, {}, available_at=datetime.now(timezone.utc) + timedelta(days=1))
    unrelated = queue.enqueue('query', 1, {'unrelated': True})
    assert queue.claim('foreground', task_id=target) is None
    assert [queue.get(item).attempts for item in (target, unrelated)] == [0, 0]


def test_corrupt_target_is_demoted_without_claiming_another_task(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    with sqlite3.connect(queue.db_path) as database:
        database.execute('UPDATE tasks SET payload_blob=? WHERE id=?', (b'{}', target))
    assert queue.claim('foreground', task_id=target) is None
    assert queue.get(target).error_code == 'payload_hash_mismatch'
    assert queue.get(target).state == 'dead'
    assert queue.get(unrelated).attempts == 0


def test_target_selection_preserves_capture_worker_ownership(tmp_path):
    queue = _queue(tmp_path)
    binding = _capture_binding(queue, _coordinator(tmp_path),
                               operational_ownership.OwnershipRegistry(tmp_path),
                               intent_id='a' * 64, intent_sha256='b' * 64)
    unrelated = queue.enqueue('query', 1, {})
    assert queue.claim('foreground', task_id=binding.task_id) is None
    assert queue.get(unrelated).attempts == 0
    assert queue.claim_capture('capture-worker').id == binding.task_id


def test_target_selection_preserves_a_live_source_fence(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    fence = queue.acquire_source_fence('2026-10-20', 'd' * 64)
    raw = canonical_json_bytes({'daily': '2026-10-20', 'source_digest': 'd' * 64})
    # Inject the racing reference past ingress, as existing source-fence tests do.
    with sqlite3.connect(queue.db_path) as database:
        database.execute('UPDATE tasks SET payload_blob=?,input_hash=? WHERE id=?',
                         (raw, sha256_bytes(raw), target))
    assert queue.claim('foreground', task_id=target) is None
    assert queue.get(unrelated).attempts == 0
    queue.release_source_fence(fence.token)
    assert queue.claim('foreground', task_id=target).id == target


def test_target_selection_cannot_override_attempt_exhaustion(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    with sqlite3.connect(queue.db_path) as database:
        database.execute('UPDATE tasks SET attempts=? WHERE id=?',
                         (memory_queue.DEFAULTS.queue_max_attempts, target))
    assert queue.claim('foreground', task_id=target) is None
    assert queue.get(unrelated).attempts == 0


def _concurrent_claim(queue, target, barrier, owner):
    barrier.wait(timeout=LONG_TIMEOUT)
    return queue.claim(owner, task_id=target)


def test_two_connections_cannot_take_the_same_target(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_concurrent_claim, queue, target, barrier, owner)
                   for owner in ('first', 'second')]
        leases = [future.result(timeout=LONG_TIMEOUT) for future in futures]
    granted = [lease for lease in leases if lease is not None]
    assert len(granted) == 1
    assert granted[0].id == target
    assert queue.get(target).attempts == 1
    assert queue.get(unrelated).attempts == 0
