"""Foreground retries retain work identity without making completion permanent."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import memory_queue
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_queue_targeted_claim import _make_ineligible
from tests.test_queue_v3_corruption import _queue


def _payload(**options):
    return {'compile_request': {'schema': 'compile-request/v1', 'file': None,
        'closed_days_only': False, 'trigger': 'manual', **options}}


def _request(queue, payload, **bounds):
    task_id, _lease = queue.claim_compile_request(payload, owner='foreground', **bounds)
    return task_id


def test_same_unfinished_request_returns_existing_task(tmp_path):
    queue = _queue(tmp_path)
    first = _request(queue, _payload())
    assert _request(queue, _payload()) == first
    assert len(queue.list_tasks()) == 1
    assert queue.get(first).attempts == 1


@pytest.mark.parametrize('state', ['leased', 'blocked', 'dead'])
def test_resume_does_not_override_state_or_attempts(tmp_path, state):
    queue = _queue(tmp_path)
    first = queue.enqueue('compile', 1, _payload())
    lease = queue.claim('original')
    _make_ineligible(queue, lease, state)
    before = queue.get(first)
    assert _request(queue, _payload()) == first
    assert queue.get(first) == before


@pytest.mark.parametrize('state', ['succeeded', 'cancelled'])
def test_terminal_work_allows_later_source_compilation(tmp_path, state):
    queue = _queue(tmp_path)
    first = queue.enqueue('compile', 1, _payload())
    _make_ineligible(queue, queue.claim('original'), state)
    second = _request(queue, _payload())
    assert second != first
    assert queue.get(second).state == 'leased'


@pytest.mark.parametrize('options', [{'file': 'knowledge/daily/2026-10-20.md'},
                                    {'closed_days_only': True}, {'trigger': 'auto'}])
def test_selection_and_trigger_are_part_of_request_identity(tmp_path, options):
    queue = _queue(tmp_path)
    first = _request(queue, _payload())
    assert _request(queue, _payload(**options)) != first


def test_explicit_redrive_leaf_is_selected_instead_of_dead_ancestor(tmp_path):
    queue = _queue(tmp_path)
    first = queue.enqueue('compile', 1, _payload())
    _make_ineligible(queue, queue.claim('original'), 'dead')
    replacement = queue.redrive(first)
    selected, lease = queue.claim_compile_request(_payload(), owner='replacement')
    assert selected == replacement
    _make_ineligible(queue, lease, 'succeeded')
    assert _request(queue, _payload()) not in (first, replacement)


@pytest.mark.parametrize('column, value', [('input_hash', '0' * 64), ('payload_blob', b'{}')])
def test_matching_corruption_refuses_instead_of_creating_another_work(tmp_path, column, value):
    queue = _queue(tmp_path)
    first = queue.enqueue('compile', 1, _payload())
    with sqlite3.connect(queue.db_path) as database:
        database.execute(f'UPDATE tasks SET {column}=? WHERE id=?', (value, first))
    with pytest.raises(memory_queue.QueueOperationError, match='dedupe_conflict'):
        _request(queue, _payload())
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_concurrent_identical_requests_create_one_task(tmp_path):
    queue = _queue(tmp_path)
    barrier = Barrier(2)

    def enqueue():
        barrier.wait(timeout=LONG_TIMEOUT)
        return queue.claim_compile_request(_payload(), owner='foreground')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue) for _ in range(2)]
        ids = [future.result(timeout=LONG_TIMEOUT) for future in futures]
    assert ids[0][0] == ids[1][0]
    assert sum(lease is not None for _task_id, lease in ids) == 1
    assert len(queue.list_tasks()) == 1


@pytest.mark.parametrize('bounds', [{'deadline': 0}, {'cancelled': lambda: True}])
def test_inactive_request_creates_no_work(tmp_path, bounds):
    queue = _queue(tmp_path)
    with pytest.raises(TimeoutError):
        _request(queue, _payload(), **bounds)
    assert queue.list_tasks() == []


def test_cancellation_inside_admission_rolls_back_new_task_and_claim(tmp_path):
    queue = _queue(tmp_path)
    checks = iter([False, True])
    with pytest.raises(TimeoutError):
        queue.claim_compile_request(_payload(), owner='foreground', cancelled=lambda: next(checks))
    assert queue.list_tasks() == []
