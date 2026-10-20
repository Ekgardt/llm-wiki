"""Only a complete source-accounted preflight may become durable progress."""
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_durable_stream_capture import _paths
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _snapshot(work, case):
    queue, lease, _ = work
    paths = _paths(case)
    digest = controller.compile_capture_digest(paths)
    dependencies = controller.CompileDependencies.capture(lambda: {'code': 'fixed'})
    root = controller.begin_compile_work(
        queue, lease, lambda: (digest, dependencies.digest, {'model': None}))
    checkpoint = replace(root, sequence=1, stage='snapshot_ready', details={})
    return controller.save_compile_capture(queue, lease, checkpoint, paths)


def _plan(reader, refused):
    return reader.iter_batches(model=None, on_refused=refused)


def _save(work, snapshot, plan=_plan, dependencies=None):
    queue, lease, _ = work
    dependencies = dependencies or controller.CompileDependencies.capture(lambda: {'code': 'fixed'})
    return controller.save_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
                                            request={'model': None}, plan=plan)


def test_complete_preflight_survives_restart_and_live_source_change(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    saved = _save(work, snapshot)
    _paths(compiler_case)[0].write_bytes(b'Changed live source after capture')
    assert _save(work, snapshot) == saved
    queue, _, _ = work
    content = queue.read_compile_artifact(saved)
    assert b'"kind":"batch"' in content
    assert content.endswith(b'"kind":"complete"}\n')
    assert saved.sequence == snapshot.sequence + 1
    assert saved.details['compile_preflight']['snapshot_digest'] == controller.progress._event_digest(snapshot)


def test_missing_source_cannot_publish_plan(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    with pytest.raises(ValueError, match='preflight.*source'):
        _save(work, snapshot, lambda _reader, _refused: iter(()))
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_duplicate_source_cannot_publish_plan(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)

    def duplicate(reader, refused):
        batch = next(_plan(reader, refused))
        yield batch
        yield batch

    with pytest.raises(ValueError, match='preflight.*source'):
        _save(work, snapshot, duplicate)
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_interrupted_planning_publishes_no_partial_progress(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    before = set(work[0].results_dir.iterdir())

    def interrupted(reader, refused):
        yield from _plan(reader, refused)
        raise OSError('preflight interrupted')

    with pytest.raises(OSError, match='preflight interrupted'):
        _save(work, snapshot, interrupted)
    assert work[0].get_compile_event(snapshot.work_id) == snapshot
    assert set(work[0].results_dir.iterdir()) == before
    assert _save(work, snapshot).sequence == snapshot.sequence + 1


def test_plan_file_recovers_after_journal_commit_failure(work, compiler_case, monkeypatch):
    snapshot = _snapshot(work, compiler_case)
    queue, _, _ = work

    def fail(*_args):
        raise OSError('event commit interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', fail)
        with pytest.raises(OSError, match='event commit interrupted'):
            _save(work, snapshot)
    assert queue.get_compile_event(snapshot.work_id) == snapshot
    saved = _save(work, snapshot)
    assert queue.get_compile_event(snapshot.work_id) == saved


def test_dependency_change_during_plan_prevents_publication(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    current = {'code': 'fixed'}
    dependencies = controller.CompileDependencies.capture(lambda: current)

    def changed(reader, refused):
        yield from _plan(reader, refused)
        current['code'] = 'second'

    with pytest.raises(ValueError, match='dependency identity changed'):
        _save(work, snapshot, changed, dependencies)
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_foreign_snapshot_is_not_a_preflight_parent(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    with pytest.raises(ValueError):
        _save(work, replace(snapshot, source_digest='a' * 64))


def test_changed_planning_request_cannot_replace_saved_plan(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    saved = _save(work, snapshot)
    dependencies = controller.CompileDependencies.capture(lambda: {'code': 'fixed'})
    with pytest.raises(ValueError, match='preflight checkpoint identity changed'):
        controller.save_compile_preflight(work[0], work[1], snapshot, dependencies=dependencies,
                                          request={'model': 'different'}, plan=_plan)
    assert work[0].get_compile_event(snapshot.work_id) == saved


def test_lost_owner_after_planning_cannot_publish_progress(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    queue, lease, _ = work

    def lost(reader, refused):
        yield from _plan(reader, refused)
        queue.cancel(lease.id)

    with pytest.raises(memory_queue.LeaseFenceError, match='lease is stale'):
        _save(work, snapshot, lost)
    assert queue.get_compile_event(snapshot.work_id) == snapshot


def test_preflight_uses_caller_deadline(work, compiler_case):
    import time

    snapshot = _snapshot(work, compiler_case)
    dependencies = controller.CompileDependencies.capture(lambda: {'code': 'fixed'})
    with pytest.raises(TimeoutError):
        controller.save_compile_preflight(work[0], work[1], snapshot, dependencies=dependencies,
                                          request={'model': None}, plan=_plan,
                                          deadline=time.monotonic() - 1)
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_recovery_rejects_changed_planning_result(work, compiler_case, monkeypatch):
    snapshot = _snapshot(work, compiler_case)
    queue, _, _ = work

    def fail(*_args):
        raise OSError('event commit interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', fail)
        with pytest.raises(OSError):
            _save(work, snapshot)

    def different(reader, refused):
        for batch in _plan(reader, refused):
            yield replace(batch, context_pending=not batch.context_pending)

    with pytest.raises(ValueError, match='preflight differs'):
        _save(work, snapshot, different)
    assert queue.get_compile_event(snapshot.work_id) == snapshot


def test_different_dependency_witness_cannot_change_original_work(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    changed = controller.CompileDependencies.capture(lambda: {'code': 'replacement'})
    with pytest.raises(ValueError, match='preflight.*identity changed'):
        _save(work, snapshot, dependencies=changed)
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_all_refused_sources_are_durable_without_a_model_call(work, compiler_case):
    import json

    from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required

    compiler, _, state = compiler_case
    case = compiler, _large_required(compiler.ROOT), state
    snapshot = _snapshot(work, case)
    saved = _save(work, snapshot)
    rows = [json.loads(line) for line in work[0].read_compile_artifact(saved).splitlines()]
    assert rows[-1] == {'kind': 'complete', 'counts': {'refused': 1}}
    assert rows[1]['kind'] == 'refused'
    assert rows[1]['reason'] == 'input_capacity'
    assert _save(work, snapshot) == saved


def test_mixed_preflight_accounts_for_refused_and_accepted_parts(work, compiler_case, monkeypatch):
    import json

    from tests.test_compile_keeps_physically_overlapping_existing_context import (
        _part_version_inputs,
    )

    compiler, _, state = compiler_case
    inputs, _ = _part_version_inputs(compiler.ROOT)
    note = compiler.ROOT / 'knowledge/notes/part-version.md'
    note.write_bytes(note.read_bytes() + b'\n' + b'z' * 38000)
    snapshot = _snapshot(work, (compiler, inputs, state))

    def forbidden(*_args):
        raise AssertionError('complete source snapshot materialized')

    monkeypatch.setattr(controller.CompileInputArchive, 'materialize', forbidden)
    monkeypatch.setattr(controller.CompileInputArchive, 'planning_inputs', forbidden)
    saved = _save(work, snapshot)
    rows = [json.loads(line) for line in work[0].read_compile_artifact(saved).splitlines()]
    assert rows[-1]['counts']['refused'] == 1
    assert rows[-1]['counts']['batch'] > 0
    with controller.open_compile_inputs(work[0], work[1], snapshot) as reader:
        expected = reader.manifest['records']['dailies']
    assert sorted(record for row in rows for record in row.get('source_records', ())) == sorted(expected)


def test_preflight_alone_does_not_authorize_model_dispatch(work, compiler_case):
    snapshot = _snapshot(work, compiler_case)
    saved = _save(work, snapshot)

    def forbidden():
        pytest.fail('a metadata plan is not a ready model request')

    with pytest.raises(ValueError, match='ready semantic checkpoint'):
        controller.run_model_call(work[0], work[1], saved, request_digest='f' * 64, invoke=forbidden)
    assert work[0].get_compile_event(snapshot.work_id) == saved


def test_redrive_recovers_the_original_preflight(work, compiler_case):
    from tests.test_compile_task_origin import _redrive

    snapshot = _snapshot(work, compiler_case)
    saved = _save(work, snapshot)
    queue, lease, checkpoint = work
    replacement = _redrive(queue, lease)
    assert _save((queue, replacement, checkpoint), snapshot) == saved
    assert queue.get_compile_event(snapshot.work_id) == saved
