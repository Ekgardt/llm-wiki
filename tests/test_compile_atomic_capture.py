"""Initial root and snapshot admission must survive capture/journal interruption."""
import json
import zipfile

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_durable_stream_capture import _paths
from tests.test_compile_preflight_consumer import _dependencies
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_streamed_archive_batches import MODEL


def _prepare(case):
    return controller.CompileCapturePreparation(_paths(case), _dependencies(), {'model': MODEL})


def _admit(work, case, prepare=None, **options):
    return controller.admit_compile_capture(work[0], work[1], prepare or (lambda: _prepare(case)), **options)


def _root_id(work):
    return controller.progress.root_work_id(work[0].compile_origin(work[1]))


def _forbidden(*args, **kwargs):
    raise AssertionError('recovery recaptured or materialized live sources')


def test_initial_capture_is_single_pass_and_recovery_uses_saved_bytes(work, compiler_case, monkeypatch):
    original = compiler_case[0].iter_compile_records
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(compiler_case[0], 'iter_compile_records', counted)
    monkeypatch.setattr(controller.CompileInputArchive, 'materialize', _forbidden)
    saved = _admit(work, compiler_case)
    root = work[0].get_compile_event(_root_id(work), sequence=0)
    assert root.source_digest == saved.source_digest
    assert calls == [True]
    _paths(compiler_case)[0].write_bytes(b'changed live source')
    assert _admit(work, compiler_case, prepare=_forbidden) == saved
    assert calls == [True]


def _interrupt_snapshot(work, monkeypatch):
    original = work[0]._append_compile_event

    def interrupted(database, row, now, *, event, lease):
        if event.sequence == 1:
            raise OSError('snapshot event interrupted')
        return original(database, row, now, event=event, lease=lease)

    monkeypatch.setattr(work[0], '_append_compile_event', interrupted)


def _orphan(work, case, monkeypatch):
    before = set(work[0].results_dir.iterdir())
    with monkeypatch.context() as patch:
        _interrupt_snapshot(work, patch)
        with pytest.raises(OSError, match='snapshot event interrupted'):
            _admit(work, case)
    assert work[0].get_compile_event(_root_id(work)) is None
    files = set(work[0].results_dir.iterdir()) - before
    assert len(files) == 1
    return files.pop()


def test_root_and_snapshot_commit_together_and_orphan_survives_live_change(work, compiler_case, monkeypatch):
    orphan = _orphan(work, compiler_case, monkeypatch)
    original_bytes = _paths(compiler_case)[0].read_bytes()
    _paths(compiler_case)[0].write_bytes(b'changed after archive publication')
    saved = _admit(work, compiler_case, prepare=_forbidden)
    assert work[0].state_root / saved.details['artifact']['reference'] == orphan
    with controller.open_compile_inputs(work[0], work[1], saved) as reader:
        assert next(reader.daily_records()).content == original_bytes


def _rewrite_orphan(path, change):
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members['manifest.json'])
    manifest['admission'].update(change)
    members['manifest.json'] = controller._snapshot_json(manifest)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)


@pytest.mark.parametrize('change', [{'origin_task_id': 'other'}, {'input_hash': 'e' * 64}])
def test_orphan_binding_cannot_move_to_another_task_or_payload(work, compiler_case, monkeypatch, change):
    orphan = _orphan(work, compiler_case, monkeypatch)
    _rewrite_orphan(orphan, change)
    with pytest.raises(ValueError, match='admission'):
        _admit(work, compiler_case, prepare=_forbidden)
    assert work[0].get_compile_event(_root_id(work)) is None
    assert orphan.exists()


def test_dependency_change_during_capture_publishes_neither_file_nor_events(work, compiler_case, monkeypatch):
    current = {'code': 'fixed'}
    dependencies = controller.CompileDependencies.capture(lambda: current)
    original = compiler_case[0].iter_compile_records

    def changed(*args, **kwargs):
        yield from original(*args, **kwargs)
        current['code'] = 'changed'

    monkeypatch.setattr(compiler_case[0], 'iter_compile_records', changed)
    prepared = controller.CompileCapturePreparation(_paths(compiler_case), dependencies, {})
    before = set(work[0].results_dir.iterdir())
    with pytest.raises(ValueError, match='dependency identity changed'):
        _admit(work, compiler_case, prepare=lambda: prepared)
    assert set(work[0].results_dir.iterdir()) == before
    assert work[0].get_compile_event(_root_id(work)) is None


def test_expired_capture_does_not_prepare_or_create_initial_work(work, compiler_case):
    with pytest.raises(TimeoutError):
        _admit(work, compiler_case, prepare=_forbidden, deadline=0)
    assert work[0].get_compile_event(_root_id(work)) is None


def test_lost_lease_after_archive_validation_cannot_admit_work(work, compiler_case, monkeypatch):
    original = controller._read_admitted_capture_root

    def lost(*args, **kwargs):
        root = original(*args, **kwargs)
        work[0].cancel(work[1].id)
        return root

    root_id = _root_id(work)
    monkeypatch.setattr(controller, '_read_admitted_capture_root', lost)
    with pytest.raises(memory_queue.LeaseFenceError):
        _admit(work, compiler_case)
    assert work[0].get_compile_event(root_id) is None


def test_admitted_capture_runs_full_work_and_queue_settlement(work, compiler_case, monkeypatch):
    from markdown_transaction import active_markdown_coordinator

    from tests.test_compile_preflight_consumer import _plan
    from tests.test_compile_saved_context_refresh import _semantic_backend

    prepared = _prepare(compiler_case)
    saved = _admit(work, compiler_case, prepare=lambda: prepared)
    calls = _semantic_backend(monkeypatch)
    completed = controller.execute_compile_work(work[0], work[1], saved,
        coordinator=active_markdown_coordinator(work[0].vault, work[0].state_root),
        dependencies=prepared.dependencies, request=prepared.request, plan=_plan, trigger='manual')
    assert completed.stage == 'published'
    assert len(calls) == 3
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'completed')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


def test_incomplete_older_admission_is_not_silently_recaptured(work, compiler_case):
    prepared = _prepare(compiler_case)
    root = controller.begin_compile_work(work[0], work[1],
        lambda: (controller.compile_capture_digest(prepared.paths), prepared.dependencies.digest, prepared.request))
    _paths(compiler_case)[0].write_bytes(b'original admitted source no longer available')
    with pytest.raises(ValueError, match='no recoverable source snapshot'):
        _admit(work, compiler_case, prepare=_forbidden)
    assert work[0].get_compile_event(root.work_id) == root


def test_corrupt_orphan_source_is_preserved_but_not_adopted(work, compiler_case, monkeypatch):
    orphan = _orphan(work, compiler_case, monkeypatch)
    with zipfile.ZipFile(orphan) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    blob = next(name for name in members if name.startswith('blobs/'))
    members[blob] = b'damaged retained bytes'
    with zipfile.ZipFile(orphan, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match='snapshot content digest mismatch'):
        _admit(work, compiler_case, prepare=_forbidden)
    assert orphan.exists()
    assert work[0].get_compile_event(_root_id(work)) is None


def test_orphan_rejects_compression_before_reading_manifest(work, compiler_case, monkeypatch):
    orphan = _orphan(work, compiler_case, monkeypatch)
    with zipfile.ZipFile(orphan) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(orphan, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    monkeypatch.setattr(zipfile.ZipFile, 'read', _forbidden)
    with pytest.raises(ValueError, match='uncompressed'):
        _admit(work, compiler_case, prepare=_forbidden)
