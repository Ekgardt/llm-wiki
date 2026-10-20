"""Streamed capture binds the admitted source identity before any model work."""
import hashlib
import time
import zipfile
from dataclasses import replace

import compile_controller as controller
import compile_progress as progress
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _paths(case):
    compiler, inputs, _ = case
    return sorted({compiler.ROOT / part.logical_path for part in inputs.dailies})


def _pending(work, digest):
    queue, lease, _ = work
    event = progress.CompileEvent('9' * 64, lease.id, 0, digest, 'pending', 'ready', {})
    queue.record_compile_event(lease, event)
    return replace(event, sequence=1, stage='snapshot_ready')


def test_capture_digest_matches_complete_snapshot(compiler_case):
    compiler, _, _ = compiler_case
    paths = _paths(compiler_case)
    expected = compiler.snapshot_compile_inputs(paths)
    assert controller.compile_capture_digest(paths) == controller.compile_inputs_digest(expected)


def test_durable_capture_opens_streamed_batches_without_materializing(work, compiler_case, monkeypatch):
    paths = _paths(compiler_case)
    digest = controller.compile_capture_digest(paths)
    checkpoint = _pending(work, digest)
    queue, lease, _ = work
    saved = controller.save_compile_capture(queue, lease, checkpoint, paths)
    with controller.open_compile_inputs(queue, lease, saved) as reader:
        assert reader.record_count('dailies') > 0
        assert list(reader.iter_batches(model=None))

    def forbid(*args, **kwargs):
        raise AssertionError('retry must not capture mutable sources again')

    monkeypatch.setattr(controller, 'write_compile_capture', forbid)
    assert controller.save_compile_capture(queue, lease, checkpoint, paths) == saved


def test_changed_source_refuses_publication_and_leaves_no_new_artifact(work, compiler_case):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    before = set(queue.results_dir.iterdir())
    paths[0].write_bytes(paths[0].read_bytes() + b'\nSource changed.\n')
    with pytest.raises(ValueError, match='snapshot.*digest'):
        controller.save_compile_capture(queue, lease, checkpoint, paths)
    assert set(queue.results_dir.iterdir()) == before
    assert queue.get_compile_event(checkpoint.work_id).stage == 'pending'


def test_capture_recovers_orphan_without_rereading_changed_live_sources(work, compiler_case, monkeypatch):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    record = queue.record_compile_event

    def interrupt(lease, event):
        raise RuntimeError('event commit interrupted')

    monkeypatch.setattr(queue, 'record_compile_event', interrupt)
    with pytest.raises(RuntimeError, match='event commit interrupted'):
        controller.save_compile_capture(queue, lease, checkpoint, paths)
    paths[0].write_bytes(b'changed after durable snapshot')
    monkeypatch.setattr(queue, 'record_compile_event', record)
    saved = controller.save_compile_capture(queue, lease, checkpoint, paths)
    with controller.open_compile_inputs(queue, lease, saved) as reader:
        assert next(reader.daily_records()).content != paths[0].read_bytes()
    assert saved.source_digest == checkpoint.source_digest


def test_capture_digest_honors_expired_caller_deadline(compiler_case):
    with pytest.raises(TimeoutError):
        controller.compile_capture_digest(_paths(compiler_case), deadline=time.monotonic() - 1)


def _orphan(queue, lease, checkpoint, paths, monkeypatch):
    def interrupt(*args):
        raise RuntimeError('event commit interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupt)
        with pytest.raises(RuntimeError, match='event commit interrupted'):
            controller.save_compile_capture(queue, lease, checkpoint, paths)
    name = hashlib.sha256(queue._compile_artifact_operation(checkpoint).encode()).hexdigest()
    return queue.results_dir / (name + '.result')


def test_orphan_with_corrupted_blob_is_retained_but_not_adopted(work, compiler_case, monkeypatch):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    orphan = _orphan(queue, lease, checkpoint, paths, monkeypatch)
    with zipfile.ZipFile(orphan) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    blob = next(name for name in members if name.startswith('blobs/'))
    members[blob] = b'corrupted physical source'
    with zipfile.ZipFile(orphan, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match='snapshot content digest mismatch'):
        controller.save_compile_capture(queue, lease, checkpoint, paths)
    assert orphan.exists()
    assert queue.get_compile_event(checkpoint.work_id).stage == 'pending'


def test_recovery_losing_lease_during_validation_cannot_publish(work, compiler_case, monkeypatch):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    orphan = _orphan(queue, lease, checkpoint, paths, monkeypatch)
    validate = controller._validate_capture_archive

    def lose_lease(*args):
        validate(*args)
        queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))

    monkeypatch.setattr(controller, '_validate_capture_archive', lose_lease)
    with pytest.raises(memory_queue.LeaseFenceError):
        controller.save_compile_capture(queue, lease, checkpoint, paths)
    assert orphan.exists()
    assert queue.get_compile_event(checkpoint.work_id).stage == 'pending'


def test_recorded_capture_rejects_changed_checkpoint_identity(work, compiler_case):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    controller.save_compile_capture(queue, lease, checkpoint, paths)
    with pytest.raises(progress.ProgressConflict, match='identity'):
        controller.save_compile_capture(queue, lease, replace(checkpoint, source_digest='0' * 64), paths)


def test_new_context_page_invalidates_precomputed_capture_digest(work, compiler_case):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    (queue.vault / 'knowledge/notes/new-context.md').write_bytes(b'---\ntype: decision\n---\nChanged context.\n')
    with pytest.raises(ValueError, match='snapshot.*digest'):
        controller.save_compile_capture(queue, lease, checkpoint, paths)
    assert queue.get_compile_event(checkpoint.work_id).stage == 'pending'


def test_capture_cannot_take_over_another_task_family(work, compiler_case):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, _, _ = work
    queue.enqueue('compile', 1, {'daily': 'unrelated capture'})
    unrelated = queue.claim('unrelated-worker')
    before = set(queue.results_dir.iterdir())
    with pytest.raises(progress.ProgressConflict, match='family'):
        controller.save_compile_capture(queue, unrelated,
                                        replace(checkpoint, task_id=unrelated.id), paths)
    assert set(queue.results_dir.iterdir()) == before
    assert queue.get_compile_event(checkpoint.work_id).stage == 'pending'


@pytest.mark.parametrize('operation', ['event', 'writer', 'stream_recovery', 'validated_recovery'])
def test_queue_progress_boundary_refuses_foreign_work(work, operation):
    queue, _, checkpoint = work
    queue.enqueue('compile', 1, {'daily': 'other family'})
    lease = queue.claim('other-family')
    event = replace(checkpoint, task_id=lease.id, sequence=checkpoint.sequence + 1,
                    stage='draft_ready', details={})
    operations = {
        'event': lambda: queue.record_compile_event(lease, event),
        'writer': lambda: queue.save_compile_artifact_writer(lease, event, lambda output: output.write(b'answer')),
        'stream_recovery': lambda: queue.recover_compile_artifact_stream(lease, event, expected_digest='a' * 64),
        'validated_recovery': lambda: queue.recover_compile_artifact_validated(lease, event, validate=lambda handle: None),
    }
    before = set(queue.results_dir.iterdir())
    with pytest.raises(progress.ProgressConflict, match='family'):
        operations[operation]()
    assert set(queue.results_dir.iterdir()) == before
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_redriven_capture_reuses_original_family_snapshot(work, compiler_case):
    paths = _paths(compiler_case)
    checkpoint = _pending(work, controller.compile_capture_digest(paths))
    queue, lease, _ = work
    saved = controller.save_compile_capture(queue, lease, checkpoint, paths)
    queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
    queue.redrive(lease.id)
    replacement = queue.claim('redriven-capture')
    paths[0].write_bytes(b'live source changed after capture')
    assert controller.save_compile_capture(queue, replacement,
                                           replace(checkpoint, task_id=replacement.id), paths) == saved
