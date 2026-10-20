"""Checkpoints use the existing queue's lease and payload boundary."""
from dataclasses import replace

import compile_progress as progress
import memory_queue
import pytest

from tests.adopted_vault import adopt


@pytest.fixture
def queue(tmp_path):
    root, state = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state)
    return queue


def event(lease, **changes):
    value = progress.CompileEvent(
        work_id='a' * 64, task_id=lease.id, sequence=0, source_digest='b' * 64,
        stage='pending', outcome='ready', details={},
    )
    return replace(value, **changes)


def lease(queue):
    queue.enqueue('compile', 1, {'daily': 'fixture'})
    return queue.claim('compile-worker')


def test_checkpoint_does_not_finish_delivery(queue):
    owner = lease(queue)
    value = event(owner)
    queue.record_compile_event(owner, value)
    assert queue.get_compile_event(value.work_id) == value
    task = queue.get(owner.id)
    assert task.state == 'leased'
    assert task.result_reference is None


def test_invalid_lease_cannot_record_a_checkpoint(queue):
    owner = lease(queue)
    with pytest.raises(memory_queue.LeaseFenceError):
        queue.record_compile_event(replace(owner, token='wrong'), event(owner))
    assert queue.get_compile_event('a' * 64) is None


def test_changed_payload_cannot_record_a_checkpoint(queue):
    owner = lease(queue)
    with queue.connection() as database:
        database.execute('UPDATE tasks SET payload_blob=? WHERE id=?',
                         (b'{"daily":"changed"}', owner.id))
    with pytest.raises(memory_queue.QueueOperationError):
        queue.record_compile_event(owner, event(owner))
    assert queue.get_compile_event('a' * 64) is None


def test_event_cannot_name_another_task(queue):
    owner = lease(queue)
    with pytest.raises(progress.ProgressConflict):
        queue.record_compile_event(owner, event(owner, task_id='other'))
    assert queue.get_compile_event('a' * 64) is None


def test_noncompile_delivery_cannot_write_compile_history(queue):
    queue.enqueue('query', 1, {'prompt': 'fixture'})
    owner = queue.claim('worker')
    with pytest.raises(progress.ProgressConflict):
        queue.record_compile_event(owner, event(owner))
    assert queue.get_compile_event('a' * 64) is None


def test_intermediate_answer_is_durable_without_terminal_result(queue):
    owner = lease(queue)
    value = event(owner)
    queue.record_compile_event(owner, value)
    saved = queue.save_compile_artifact(
        owner, event(owner, sequence=1, stage='draft_ready'), b'validated transport response',
    )
    assert queue.read_compile_artifact(saved) == b'validated transport response'
    assert queue.get_compile_event(value.work_id) == saved
    assert queue.get(owner.id).result_reference is None


def test_corrupted_intermediate_answer_is_refused(queue):
    owner = lease(queue)
    saved = queue.save_compile_artifact(owner, event(owner), b'answer')
    path = queue.state_root / saved.details['artifact']['reference']
    path.write_bytes(b'changed')
    with pytest.raises(progress.ProgressCorrupt):
        queue.read_compile_artifact(saved)


def test_invalid_owner_creates_no_intermediate_artifact(queue):
    owner = lease(queue)
    before = set(queue.results_dir.glob('*'))
    with pytest.raises(memory_queue.LeaseFenceError):
        queue.save_compile_artifact(replace(owner, token='wrong'), event(owner), b'answer')
    assert set(queue.results_dir.glob('*')) == before


def test_artifact_reference_cannot_escape_queue_results(queue):
    owner = lease(queue)
    value = event(owner, details={'artifact': {
        'reference': '../secret', 'sha256': 'c' * 64,
    }})
    with pytest.raises(progress.ProgressCorrupt):
        queue.read_compile_artifact(value)


def test_saved_answer_is_recovered_after_checkpoint_failure(queue, monkeypatch):
    owner = lease(queue)
    value = event(owner)
    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', _crash_before_checkpoint)
        with pytest.raises(RuntimeError, match='injected'):
            queue.save_compile_artifact(owner, value, b'complete answer')
    assert queue.get_compile_event(value.work_id) is None
    recovered = queue.recover_compile_artifact(owner, value)
    assert queue.read_compile_artifact(recovered) == b'complete answer'
    assert queue.get_compile_event(value.work_id) == recovered


def _crash_before_checkpoint(*args):
    raise RuntimeError('injected crash after durable answer')


def test_absent_answer_is_not_replaced_with_empty_success(queue):
    owner = lease(queue)
    assert queue.recover_compile_artifact(owner, event(owner)) is None
    assert queue.get_compile_event('a' * 64) is None


def test_missing_artifact_cannot_be_bound_to_progress(queue):
    owner = lease(queue)
    value = event(owner, details={'artifact': {
        'reference': 'run/queue-results/' + 'c' * 64 + '.result', 'sha256': 'd' * 64,
    }})
    with pytest.raises(progress.ProgressCorrupt):
        queue.record_compile_event(owner, value)
    assert queue.get_compile_event(value.work_id) is None
