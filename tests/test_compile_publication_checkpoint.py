"""Retain the accepted publication plan before any Markdown side effect."""
from contextlib import contextmanager
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_saved_context_refresh import _semantic_backend


@contextmanager
def _resolved(work, case, monkeypatch):
    from compile_cache import CompileCache
    from markdown_transaction import active_markdown_coordinator

    _, inputs, state = case
    snapshot = _snapshot(work, inputs)
    items = _consume(work, snapshot)
    item = next(items)
    calls = _semantic_backend(monkeypatch)
    coordinator = active_markdown_coordinator(work[0].vault, state)
    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot,
            dependencies=_dependencies()) as (ready, context):
        replay = controller.CompileReplay(work[0], work[1], context, dependencies=_dependencies())
        resolved = compiler.resolve_compile_plan(ready.inputs, CompileCache(state),
            coordinator=coordinator, batch=ready, call_runner=replay)
        yield ready, context, resolved, coordinator, calls
    items.close()


def _save(work, ready, context, resolved):
    return controller.save_compile_publication(work[0], work[1], context, ready, resolved,
        dependencies=_dependencies())


def test_accepted_plan_is_saved_and_cache_hit_does_not_change_it(work, compiler_case, monkeypatch):
    from compile_cache import CompileCache

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        assert saved.stage == 'publication_ready'
        assert saved.outcome == 'ready'
        cached = compiler.resolve_compile_plan(ready.inputs, CompileCache(compiler_case[2]),
            coordinator=coordinator, batch=ready,
            call_runner=controller.CompileReplay(work[0], work[1], context, dependencies=_dependencies()))
        assert cached.cache_hit
        assert _save(work, ready, context, cached) == saved
        document = controller.read_compile_publication(work[0], work[1], saved)
        assert document['plan'] == resolved.plan
        assert document['action_key'] == resolved.action_key
        assert len(calls) == 3
        assert not (work[0].vault / 'knowledge/notes/cedar-merge-order.md').exists()


def test_publication_orphan_keeps_original_timestamp(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, _, _calls):
        def interrupted(*_args):
            raise OSError('publication event lost')

        with monkeypatch.context() as patch:
            patch.setattr(work[0], 'record_compile_event', interrupted)
            with pytest.raises(OSError, match='publication event'):
                _save(work, ready, context, resolved)

        def forbidden():
            raise AssertionError('recovery created a new publication timestamp')

        monkeypatch.setattr(compiler, '_utc_now', forbidden)
        saved = _save(work, ready, context, resolved)
        assert controller.read_compile_publication(work[0], work[1], saved)['completed_at'].endswith('Z')


def test_changed_accepted_plan_cannot_replace_publication(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, _, _calls):
        saved = _save(work, ready, context, resolved)
        changed = replace(resolved, action_key='e' * 64)
        with pytest.raises(ValueError, match='publication.*identity'):
            _save(work, ready, context, changed)
        assert work[0].get_compile_event(context.work_id) == saved


def test_publication_reader_rejects_unrecorded_event(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, _, _calls):
        saved = _save(work, ready, context, resolved)
        with pytest.raises(ValueError, match='recorded publication'):
            controller.read_compile_publication(work[0], work[1], replace(saved, outcome='completed'))


def _apply_saved(work, ready, saved, coordinator):
    document = controller.read_compile_publication(work[0], work[1], saved)
    return compiler.apply_compile_plan(ready.inputs, document['plan'],
        action_key=document['action_key'], trigger='manual', coordinator=coordinator,
        batch=ready, provider_budget=document['provider_budget'], completed_at=document['completed_at'])


def test_saved_plan_publication_recovers_from_receipts_without_second_write(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        first = _apply_saved(work, ready, saved, coordinator)
        note = work[0].vault / 'knowledge/notes/cedar-merge-order.md'
        content, modified = note.read_bytes(), note.stat().st_mtime_ns
        second = _apply_saved(work, ready, saved, coordinator)
        assert first.state == second.state == 'committed'
        assert first.transaction_id == second.transaction_id
        assert first.commit_sequence == second.commit_sequence
        assert (note.read_bytes(), note.stat().st_mtime_ns) == (content, modified)
        assert second.touched == ()
        assert len(calls) == 3
        assert work[0].get_compile_event(context.work_id).stage == 'publication_ready'


def test_unresolved_model_dispatch_cannot_be_skipped_by_publication(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, _, _calls):
        queue, lease, _ = work
        latest = queue.get_compile_event(context.work_id)
        dispatch = controller._dispatch_event(latest, lease.id, 'e' * 64)
        queue.record_compile_event(lease, dispatch)
        with pytest.raises(ValueError, match='ready semantic checkpoint'):
            _save(work, ready, context, resolved)
        assert queue.get_compile_event(context.work_id) == dispatch


def test_publication_survives_redrive_and_actual_cached_resolution(work, compiler_case, monkeypatch):
    from compile_cache import CompileCache

    from tests.test_compile_task_origin import _redrive

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        source = context.details['compile_context']
        snapshot = work[0].get_compile_event(source['snapshot_work_id'], sequence=source['snapshot_sequence'])
    replacement = _redrive(work[0], work[1])
    resumed = (work[0], replacement, work[2])
    items = _consume(resumed, snapshot)
    with controller.open_prepared_compile_batch(work[0], replacement, next(items), snapshot,
            dependencies=_dependencies()) as (ready, context):
        resolved = compiler.resolve_compile_plan(ready.inputs, CompileCache(compiler_case[2]),
            coordinator=coordinator, batch=ready,
            call_runner=controller.CompileReplay(work[0], replacement, context, dependencies=_dependencies()))
        assert resolved.cache_hit
        assert _save(resumed, ready, context, resolved) == saved
        assert controller.read_compile_publication(work[0], replacement, saved)['plan'] == resolved.plan
        assert len(calls) == 3
    items.close()


@pytest.mark.parametrize('damage', ['timestamp', 'content'])
def test_damaged_publication_orphan_is_retained_and_refused(work, compiler_case, monkeypatch, damage):
    import hashlib
    import json

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, _, _calls):
        latest = work[0].get_compile_event(context.work_id)
        name = hashlib.sha256(f'compile:{latest.work_id}:{latest.sequence + 1}'.encode()).hexdigest()
        path = work[0].results_dir / (name + '.result')

        def interrupted(*_args):
            raise OSError('journal unavailable')

        with monkeypatch.context() as patch:
            patch.setattr(work[0], 'record_compile_event', interrupted)
            with pytest.raises(OSError):
                _save(work, ready, context, resolved)
        document = json.loads(path.read_bytes())
        if damage == 'timestamp':
            document['completed_at'] = 'not a timestamp'
        else:
            document['content']['action_key'] = 'f' * 64
        path.write_bytes(controller._snapshot_json(document))
        with pytest.raises(ValueError):
            _save(work, ready, context, resolved)
        assert path.exists()
        assert work[0].get_compile_event(context.work_id) == latest
