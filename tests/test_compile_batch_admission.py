"""Batch work is admitted only from an exact retained preflight record."""
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded


def _begin(work, item, dependencies=None):
    return controller.begin_compile_batch(work[0], work[1], item,
        dependencies=dependencies or _dependencies())


def test_batch_admission_is_durable_and_reused_before_context_capture(work, monkeypatch):
    import compile_memory

    def forbidden(*_args):
        raise AssertionError('admission read mutable context')

    monkeypatch.setattr(compile_memory, 'snapshot_compile_inputs', forbidden)
    items = _consume(work, _snapshot(work))
    item = next(items)
    first = _begin(work, item)
    assert first == work[0].get_compile_event(item.work_id, sequence=0)
    assert first.stage == 'pending'
    assert _begin(work, item) == first
    items.close()


@pytest.mark.parametrize('field,value', [('work_id', 'a' * 64), ('entry_digest', 'b' * 64),
                                         ('entry_offset', 0), ('entry_offset', -1)])
def test_changed_plan_identity_is_not_admitted(work, field, value):
    items = _consume(work, _snapshot(work))
    item = next(items)
    with pytest.raises(ValueError):
        _begin(work, replace(item, **{field: value}))
    assert work[0].get_compile_event(item.work_id) is None
    items.close()


def test_changed_batch_contents_are_not_admitted(work):
    items = _consume(work, _snapshot(work))
    item = next(items)
    changed = replace(item.batch, context_pending=not item.batch.context_pending)
    with pytest.raises(ValueError):
        _begin(work, replace(item, batch=changed))
    items.close()


def test_batch_admission_survives_redrive(work):
    from tests.test_compile_task_origin import _redrive

    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    first = _begin(work, next(items))
    items.close()
    replacement = _redrive(work[0], work[1])
    resumed = (work[0], replacement, work[2])
    items = _consume(resumed, snapshot)
    assert _begin(resumed, next(items)) == first
    items.close()


def test_changed_dependency_blocks_admission(work):
    items = _consume(work, _snapshot(work))
    item = next(items)
    dependencies = controller.CompileDependencies.capture(lambda: {'code': 'different'})
    with pytest.raises(ValueError, match='dependenc'):
        _begin(work, item, dependencies)
    assert work[0].get_compile_event(item.work_id) is None
    items.close()


def test_lost_owner_cannot_admit_batch(work):
    items = _consume(work, _snapshot(work))
    item = next(items)
    work[0].cancel(work[1].id)
    with pytest.raises(memory_queue.LeaseFenceError):
        _begin(work, item)
    items.close()


def test_unrecorded_parent_is_not_trusted(work):
    items = _consume(work, _snapshot(work))
    item = next(items)
    changed = replace(item.preflight, outcome='completed')
    with pytest.raises(ValueError, match='recorded preflight'):
        _begin(work, replace(item, preflight=changed))
    items.close()


def test_existing_conflicting_batch_is_not_silently_reused(work):
    items = _consume(work, _snapshot(work))
    item = next(items)
    forged = controller.progress.CompileEvent(item.work_id, work[1].id, 0,
        'a' * 64, 'pending', 'ready', {})
    work[0].record_compile_event(work[1], forged)
    with pytest.raises(ValueError, match='admission identity'):
        _begin(work, item)
    items.close()


def test_dependency_change_during_artifact_verification_does_not_publish(work, monkeypatch):
    from contextlib import contextmanager

    items = _consume(work, _snapshot(work))
    item = next(items)
    current = {'code': 'fixed'}
    dependencies = controller.CompileDependencies.capture(lambda: current)
    original = work[0].open_compile_artifact

    @contextmanager
    def changed(*args, **kwargs):
        with original(*args, **kwargs) as handle:
            yield handle
        current['code'] = 'changed'

    monkeypatch.setattr(work[0], 'open_compile_artifact', changed)
    with pytest.raises(ValueError, match='dependency identity'):
        _begin(work, item, dependencies)
    assert work[0].get_compile_event(item.work_id) is None
    items.close()
