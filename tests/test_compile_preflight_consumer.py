"""Batch consumption must match a complete retained plan before any work escapes."""
from dataclasses import replace

import compile_controller as controller
import pytest
import queue_core
from llm_wiki import runtime_roots

from tests.compile_call_controller_helpers import work as work
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL
from tests.compile_preflight_consumer_helpers import _consume, _plan, _snapshot
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_streamed_archive_batches_helpers import _materialized


def test_complete_plan_is_durable_before_first_batch_is_exposed(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    first = next(items)
    saved = work[0].get_compile_event(snapshot.work_id)
    assert saved == first.preflight
    assert work[0].read_compile_artifact(saved).endswith(b'"kind":"complete"}\n')
    assert first.work_id != saved.work_id
    assert len(list(items)) == 2


def test_batch_identities_and_contents_survive_replay(work):
    snapshot = _snapshot(work)
    first = [(_materialized(item.batch), item.work_id, item.entry_digest) for item in _consume(work, snapshot)]
    second = [(_materialized(item.batch), item.work_id, item.entry_digest) for item in _consume(work, snapshot)]
    assert first == second
    assert len({item[1] for item in first}) == 3


def test_incomplete_planning_exposes_no_batch(work):
    snapshot = _snapshot(work)

    def interrupted(reader, refused):
        yield next(_plan(reader, refused))
        raise OSError('incomplete preflight')

    with pytest.raises(OSError, match='incomplete preflight'):
        next(_consume(work, snapshot, plan=interrupted))
    assert work[0].get_compile_event(snapshot.work_id) == snapshot


def test_changed_batch_during_consumer_replay_is_not_exposed(work):
    snapshot = _snapshot(work)
    calls = []

    def changing(reader, refused):
        calls.append(True)
        for batch in _plan(reader, refused):
            if len(calls) > 1:
                batch = replace(batch, context_pending=not batch.context_pending)
            yield batch

    with pytest.raises(ValueError, match='preflight.*record'):
        next(_consume(work, snapshot, plan=changing))


def test_lease_loss_between_batches_stops_before_next_yield(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    next(items)
    work[0].cancel(work[1].id)
    with pytest.raises(queue_core.LeaseFenceError):
        next(items)


def test_dependency_change_between_batches_stops_before_next_yield(work):
    snapshot = _snapshot(work)
    current = {'code': 'fixed'}
    dependencies = controller.CompileDependencies.capture(lambda: current)
    items = _consume(work, snapshot, dependencies=dependencies)
    next(items)
    current['code'] = 'changed'
    with pytest.raises(ValueError, match='dependency identity changed'):
        next(items)


def test_consumer_does_not_materialize_the_archive(work, monkeypatch):
    snapshot = _snapshot(work)

    def forbidden(*_args):
        raise AssertionError('whole archive materialized')

    monkeypatch.setattr(controller.CompileInputArchive, 'materialize', forbidden)
    monkeypatch.setattr(controller.CompileInputArchive, 'planning_inputs', forbidden)
    assert len(list(_consume(work, snapshot))) == 3


def test_consumer_releases_previous_daily_records(work):
    import weakref

    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    first = next(items)
    reference = weakref.ref(first.batch.inputs.dailies[0])
    del first
    second = next(items)
    assert second.batch.inputs.dailies
    assert reference() is None
    items.close()


def test_batch_identities_survive_task_redrive(work):
    from tests.compile_task_origin_helpers import _redrive

    snapshot = _snapshot(work)
    original = [item.work_id for item in _consume(work, snapshot)]
    queue, lease, checkpoint = work
    replacement = _redrive(queue, lease)
    replay = [item.work_id for item in _consume((queue, replacement, checkpoint), snapshot)]
    assert replay == original


def test_refusal_effects_happen_only_after_the_complete_plan_is_durable(work, compiler_case):
    from tests.compile_keeps_physically_overlapping_existing_context_helpers import _large_required

    inputs = _large_required(runtime_roots.vault_root())
    snapshot = _snapshot(work, inputs)
    observed = []

    def refused(unit):
        saved = work[0].get_compile_event(snapshot.work_id)
        assert 'compile_preflight' in saved.details
        assert work[0].read_compile_artifact(saved).endswith(b'"kind":"complete"}\n')
        from tests.compile_streamed_archive_batches_helpers import _materialized_daily

        observed.append(tuple(_materialized_daily(part) for part in unit.dailies))

    assert list(_consume(work, snapshot, on_refused=refused)) == []
    assert observed == [inputs.dailies]


def test_refusal_callback_failure_retains_the_plan_for_recovery(work, compiler_case):
    from tests.compile_keeps_physically_overlapping_existing_context_helpers import _large_required

    snapshot = _snapshot(work, _large_required(runtime_roots.vault_root()))

    def refused(_unit):
        raise OSError('source diagnostic write failed')

    with pytest.raises(OSError, match='source diagnostic write failed'):
        list(_consume(work, snapshot, on_refused=refused))
    saved = work[0].get_compile_event(snapshot.work_id)
    observed = []
    assert list(_consume(work, snapshot, on_refused=observed.append)) == []
    assert len(observed) == 1
    assert work[0].get_compile_event(snapshot.work_id) == saved


def test_closing_consumer_closes_owned_archive_and_plan_handles(work, monkeypatch):
    from contextlib import contextmanager

    snapshot = _snapshot(work)
    original = work[0].open_compile_artifact
    handles = []

    @contextmanager
    def tracked(event, **options):
        with original(event, **options) as handle:
            handles.append(handle)
            yield handle

    monkeypatch.setattr(work[0], 'open_compile_artifact', tracked)
    items = _consume(work, snapshot)
    next(items)
    assert any(not handle.closed for handle in handles)
    items.close()
    assert all(handle.closed for handle in handles)


def test_native_projection_is_rebuilt_from_saved_physical_sources(work, tmp_path, monkeypatch):
    from llm_wiki import compile_pipeline_batches, compile_pipeline_prompts

    from tests.native_compile_uses_whole_container_helpers import _native_inputs

    prompt = 'The user requires exact native evidence. ' * 50
    inputs, _ = _native_inputs(tmp_path, monkeypatch, prompt)
    snapshot = _snapshot(work, inputs)
    expected = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL)
    actual = tuple(_materialized(item.batch) for item in _consume(work, snapshot))
    assert actual == expected
    assert any(part.native_frames for batch in actual for part in batch.inputs.dailies)
    assert prompt in compile_pipeline_prompts.draft_prompt(actual[0].inputs)


def test_returned_event_cannot_mutate_the_remaining_consumer_identity(work):
    snapshot = _snapshot(work)
    expected = [item.work_id for item in _consume(work, snapshot)]
    items = _consume(work, snapshot)
    first = next(items)
    first.preflight.details['compile_preflight']['request_digest'] = 'f' * 64
    assert [first.work_id, *(item.work_id for item in items)] == expected
