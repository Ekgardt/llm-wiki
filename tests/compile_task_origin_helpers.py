"""Helpers shared by test_compile_task_origin.py and the test files importing them."""

import queue_core


def _redrive(queue, lease):
    queue.fail(lease, queue_core.QueueFailure('worker_failed', permanent=True))
    queue.redrive(lease.id)
    return queue.claim('origin-redrive')
