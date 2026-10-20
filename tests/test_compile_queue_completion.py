"""A truthy processor response cannot complete unproven compile work."""
import time
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_work_completion import _confirm, _execute, _refuse_first
from tests.test_compile_work_completion import multi as multi


def _publish(work):
    return work[0].publish_result(work[1], operation_id=work[1].id, result=b'compile finished')


@pytest.mark.parametrize('action', [_publish, lambda work: work[0].acknowledge(work[1])])
def test_missing_completion_refuses_before_terminal_mutation(work, action):
    before = set(work[0].results_dir.iterdir())
    with pytest.raises(memory_queue.QueueOperationError) as caught:
        action(work)
    assert caught.value.code == 'compile_work_incomplete'
    assert work[0].get(work[1].id).state == 'leased'
    assert work[0].get(work[1].id).result_reference is None
    assert set(work[0].results_dir.iterdir()) == before


def test_partial_compile_cannot_publish_success(work, multi):
    assert _execute(work, multi, plan=_refuse_first) is None
    with pytest.raises(memory_queue.QueueOperationError, match='compile_work_incomplete'):
        _publish(work)
    assert work[0].get(work[1].id).result_reference is None


def test_complete_real_compile_can_publish_and_acknowledge(work, multi):
    assert _execute(work, multi).stage == 'published'
    _publish(work)
    assert work[0].acknowledge(work[1]).state == 'succeeded'


def _truthy_processor(_task):
    return True


def _worker_settle(work):
    counts = {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    memory_queue._run_and_settle(work[0], _truthy_processor,
        memory_queue._run_processor_child, work[1], counts,
        deadline=time.monotonic() + 60, monotonic=time.monotonic,
        lease_seconds=memory_queue.DEFAULTS.queue_lease_seconds,
        heartbeat_seconds=memory_queue.DEFAULTS.queue_heartbeat_seconds,
        max_attempts=memory_queue.DEFAULTS.queue_max_attempts,
        retry_base_seconds=memory_queue.DEFAULTS.retry_base_seconds,
        retry_cap_seconds=memory_queue.DEFAULTS.retry_cap_seconds)
    return counts


def test_worker_blocks_truthy_response_without_compile_proof(work):
    counts = _worker_settle(work)
    assert counts['ok'] == 0
    assert counts['failed'] == 1
    task = work[0].get(work[1].id)
    assert task.state == 'blocked'
    assert task.blocked_capability == 'compile_completion'
    assert task.error_code == 'compile_work_incomplete'


def test_worker_settles_real_full_compile_after_spawned_response(work, multi):
    _execute(work, multi)
    counts = _worker_settle(work)
    assert counts['ok'] == 1
    assert counts['failed'] == 0
    assert work[0].get(work[1].id).state == 'succeeded'


@pytest.mark.parametrize('change', [
    {'schema': 'wrong'}, {'batch_count': -1}, {'children_digest': 'wrong'},
    {'preflight_digest': 'e' * 64},
])
def test_malformed_completion_attestation_is_not_terminal_authority(work, multi, change):
    assert _confirm(work, multi) is None
    parent = work[0].get_compile_event(multi[0].work_id)
    proof = {'schema': 'compile-work-completion/v1', 'batch_count': 2,
             'children_digest': 'a' * 64, 'preflight_digest': controller.progress._event_digest(parent)}
    proof.update(change)
    completion = replace(parent, sequence=parent.sequence + 1, stage='published', outcome='completed',
                         details={'work_completion': proof})
    work[0].record_compile_event(work[1], completion)
    with pytest.raises(memory_queue.QueueOperationError, match='compile_completion_corrupt'):
        _publish(work)
    assert work[0].get(work[1].id).result_reference is None


def _advance_work(work, completed):
    work[0].record_compile_event(work[1], replace(completed, sequence=completed.sequence + 1,
        stage='pending', outcome='ready', details={'unexpected_work_after_completion': True}))


def test_completion_is_checked_again_when_result_bytes_are_bound(work, multi, monkeypatch):
    completed = _execute(work, multi)
    store = work[0]._store_result_bytes

    def advanced(operation_id, result):
        stored = store(operation_id, result)
        _advance_work(work, completed)
        return stored

    monkeypatch.setattr(work[0], '_store_result_bytes', advanced)
    with pytest.raises(memory_queue.QueueOperationError, match='compile_work_incomplete'):
        _publish(work)
    assert work[0].get(work[1].id).result_reference is None


def test_published_result_does_not_bypass_acknowledgement_check(work, multi):
    completed = _execute(work, multi)
    _publish(work)
    _advance_work(work, completed)
    with pytest.raises(memory_queue.QueueOperationError, match='compile_work_incomplete'):
        work[0].acknowledge(work[1])
    assert work[0].get(work[1].id).state == 'leased'
