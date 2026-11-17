"""Helpers shared by test_queue_targeted_claim.py and the test files importing them."""

import queue_core

from tests.queue_v3_corruption_helpers import _queue


def _pair(tmp_path):
    queue = _queue(tmp_path)
    target = queue.enqueue('query', 1, {'query': 'requested'})
    unrelated = queue.enqueue('query', 1, {'query': 'unrelated'}, priority=100)
    return queue, target, unrelated


def _finish(queue, lease):
    queue.publish_result(lease, operation_id=lease.id, result=b'done')
    queue.acknowledge(lease)


def _make_ineligible(queue, lease, state):
    actions = {
        'leased': lambda: None,
        'blocked': lambda: queue.fail(lease, queue_core.QueueFailure(
            'unknown', blocked_capability='provider_outcome_unknown')),
        'dead': lambda: queue.fail(lease, queue_core.QueueFailure('failed', permanent=True)),
        'cancelled': lambda: queue.cancel(lease.id),
        'succeeded': lambda: _finish(queue, lease),
    }
    actions[state]()
