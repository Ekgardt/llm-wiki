"""Retain exact refreshed context before model work; never recapture on recovery."""
from dataclasses import replace

import compile_controller as controller
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_context_delta import _context, _materialized, _records
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _save(work, item, snapshot, capture=None, dependencies=None):
    return controller.save_compile_batch_context(work[0], work[1], item, snapshot,
        dependencies=dependencies or _dependencies(), capture=capture or (lambda: _records(_context())))


def _open(work, item, snapshot, saved):
    return controller.open_compile_batch_context(work[0], work[1], item, snapshot, saved,
        dependencies=_dependencies())


def _forbidden():
    raise AssertionError('recovery reread mutable context')


def test_batch_context_survives_restart_without_live_recapture(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    saved = _save(work, item, snapshot)
    assert saved.stage == 'snapshot_ready'
    assert _save(work, item, snapshot, _forbidden) == saved
    with _open(work, item, snapshot, saved) as view:
        assert _materialized(view) == _context()
    items.close()


def test_context_orphan_recovers_without_live_recapture(work, monkeypatch):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    controller.begin_compile_batch(work[0], work[1], item, dependencies=_dependencies())

    def interrupted(*_args):
        raise OSError('context event commit failed')

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError, match='context event commit'):
            _save(work, item, snapshot)
    saved = _save(work, item, snapshot, _forbidden)
    with _open(work, item, snapshot, saved) as view:
        assert _materialized(view) == _context()
    items.close()


def test_partial_capture_does_not_publish_context(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)

    def interrupted():
        yield next(_records(_context()))
        raise OSError('source unreadable')

    with pytest.raises(OSError, match='source unreadable'):
        _save(work, item, snapshot, interrupted)
    assert work[0].get_compile_event(item.work_id).sequence == 0
    assert _save(work, item, snapshot).sequence == 1
    items.close()


def test_context_cannot_be_reused_for_another_batch(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    first = next(items)
    saved = _save(work, first, snapshot)
    second = next(items)
    with pytest.raises(ValueError):
        with _open(work, second, snapshot, saved):
            pytest.fail('foreign context accepted')
    items.close()


def test_context_survives_redrive(work):
    from tests.test_compile_task_origin import _redrive

    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    saved = _save(work, next(items), snapshot)
    items.close()
    replacement = _redrive(work[0], work[1])
    resumed = (work[0], replacement, work[2])
    items = _consume(resumed, snapshot)
    item = next(items)
    assert _save(resumed, item, snapshot, _forbidden) == saved
    with _open(resumed, item, snapshot, saved) as view:
        assert _materialized(view) == _context()
    items.close()


def test_dependency_change_during_capture_prevents_publication(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    current = {'code': 'fixed'}
    dependencies = controller.CompileDependencies.capture(lambda: current)

    def changed():
        yield from _records(_context())
        current['code'] = 'changed'

    with pytest.raises(ValueError, match='dependency identity'):
        _save(work, item, snapshot, changed, dependencies)
    assert work[0].get_compile_event(item.work_id).sequence == 0
    items.close()


def test_wrong_source_base_is_rejected_before_capture(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    with pytest.raises(ValueError):
        _save(work, next(items), replace(snapshot, source_digest='f' * 64), _forbidden)
    items.close()


def test_invalid_current_record_is_not_published(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    bad = replace(_context().sources[0], sha256='e' * 64)
    with pytest.raises(ValueError, match='digest'):
        _save(work, item, snapshot, lambda: iter([('sources', bad)]))
    assert work[0].get_compile_event(item.work_id).sequence == 0
    items.close()


def _context_orphan_path(queue, work_id):
    import hashlib

    name = hashlib.sha256(f'compile:{work_id}:1'.encode()).hexdigest()
    return queue.results_dir / (name + '.result')


def test_foreign_context_file_cannot_be_adopted_as_orphan(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    first = next(items)
    saved = _save(work, first, snapshot)
    second = next(items)
    controller.begin_compile_batch(work[0], work[1], second, dependencies=_dependencies())
    raw = work[0].read_compile_artifact(saved)
    orphan = _context_orphan_path(work[0], second.work_id)
    orphan.write_bytes(raw)
    orphan.chmod(0o600)
    with pytest.raises(ValueError, match='context delta base identity'):
        _save(work, second, snapshot, _forbidden)
    assert work[0].get_compile_event(second.work_id).sequence == 0
    assert orphan.exists()
    items.close()


def test_corrupted_orphan_is_kept_but_not_adopted(work, monkeypatch):
    import zipfile

    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    controller.begin_compile_batch(work[0], work[1], item, dependencies=_dependencies())

    def interrupted(*_args):
        raise OSError('context event commit failed')

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError):
            _save(work, item, snapshot)
    orphan = _context_orphan_path(work[0], item.work_id)
    with zipfile.ZipFile(orphan) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    blob = next(name for name in entries if name.startswith('blobs/'))
    entries[blob] = b'corrupted'
    with zipfile.ZipFile(orphan, 'w') as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    with pytest.raises(ValueError, match='digest'):
        _save(work, item, snapshot, _forbidden)
    assert orphan.exists()
    assert work[0].get_compile_event(item.work_id).sequence == 0
    items.close()


def test_source_record_in_target_group_is_rejected_before_publication(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    with pytest.raises(ValueError, match='context record type'):
        _save(work, item, snapshot, lambda: iter([('targets', _context().sources[0])]))
    assert work[0].get_compile_event(item.work_id).sequence == 0
    items.close()


def test_default_capture_reads_real_compiler_context_stream(work, compiler_case):
    compiler, _, _ = compiler_case
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    expected = compiler.snapshot_compile_inputs(())
    saved = controller.save_compile_batch_context(work[0], work[1], item, snapshot,
        dependencies=_dependencies())
    with _open(work, item, snapshot, saved) as view:
        assert _materialized(view) == expected
    items.close()


def test_lost_owner_during_context_capture_cannot_publish(work):
    import memory_queue

    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)

    def cancelled():
        yield from _records(_context())
        work[0].cancel(work[1].id)

    with pytest.raises(memory_queue.LeaseFenceError):
        _save(work, item, snapshot, cancelled)
    assert work[0].get_compile_event(item.work_id).sequence == 0
    items.close()
