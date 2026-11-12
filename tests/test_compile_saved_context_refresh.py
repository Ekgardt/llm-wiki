"""Saved context uses the compiler's normal selection and identity checks."""
from dataclasses import replace

import compile_controller as controller
import pytest
from llm_wiki import compile_pipeline_batches, compile_pipeline_prompts, compile_pipeline_sources

from tests.compile_call_controller_helpers import work as work
from tests.compile_context_delta_helpers import _context, _records
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _consume, _dependencies, _snapshot
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_refresh_selects_context_once_helpers import _planned
from tests.compile_saved_context_refresh_helpers import _semantic_backend
from tests.compile_streamed_archive_batches_helpers import _materialized


def test_saved_context_matches_existing_refresh_without_live_capture(monkeypatch):
    batch = _planned(monkeypatch)
    context = _context()
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', lambda _paths: context)
    expected = compile_pipeline_batches.refresh_compile_batch(batch)

    def forbidden(*_args):
        raise AssertionError('saved refresh reread live context')

    point_compile_path(monkeypatch, 'snapshot_compile_inputs', forbidden)
    actual = compile_pipeline_batches.refresh_compile_batch_from_context(batch, context)
    assert actual == expected
    assert not actual.context_pending


def test_combining_context_does_not_load_its_bodies(monkeypatch):
    batch = _planned(monkeypatch)

    class MetadataOnly:
        def logical_paths(self):
            return ('knowledge/notes/accepted.md',)

        def __getitem__(self, _index):
            raise AssertionError('context body loaded during assembly')

    context = replace(_context(), sources=MetadataOnly())
    joined = compile_pipeline_batches.compile_batch_context_inputs(batch, context)
    assert joined.sources.logical_paths() == (
        batch.inputs.dailies[0].logical_path, 'knowledge/notes/accepted.md')
    selected = joined.sources.select_paths({batch.inputs.dailies[0].logical_path})
    assert selected[0].content == batch.inputs.dailies[0].content


def test_prepared_saved_batch_replays_after_current_context_changes(work):
    snapshot = _snapshot(work)
    items = _consume(work, snapshot)
    item = next(items)
    options = dict(dependencies=_dependencies(), capture=lambda: _records(_context()))
    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot, **options) as (ready, saved):
        first = _materialized(ready)
        assert not ready.context_pending

    def forbidden():
        raise AssertionError('saved preparation reread live pages')

    options['capture'] = forbidden
    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot, **options) as (ready, resumed):
        assert _materialized(ready) == first
        assert resumed == saved
    items.close()


@pytest.mark.parametrize('field', ['manifest', 'model', 'candidates', 'budget'])
def test_saved_refresh_keeps_existing_identity_guards(monkeypatch, field):
    from tests.compile_refresh_selects_context_once_helpers import _alter_refresh

    batch = _planned(monkeypatch)
    original = compile_pipeline_batches.pack_compile_batches

    def altered(inputs, **kwargs):
        return (_alter_refresh(original(inputs, **kwargs)[0], field),)

    point_compile_path(monkeypatch, 'pack_compile_batches', altered)
    with pytest.raises(ValueError, match='changed while refreshing context'):
        compile_pipeline_batches.refresh_compile_batch_from_context(batch, _context())


def _resolve_saved(work, item, snapshot, state, coordinator, capture):
    from compile_cache import CompileCache

    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot,
            dependencies=_dependencies(), capture=capture) as (ready, checkpoint):
        replay = controller.CompileReplay(work[0], work[1], checkpoint, dependencies=_dependencies())
        return compile_pipeline_batches.resolve_compile_plan(ready.inputs, CompileCache(state), coordinator=coordinator,
                                              batch=ready, call_runner=replay)


def test_real_semantic_replay_uses_saved_context_and_draft_after_interruption(work, compiler_case, monkeypatch):
    from markdown_transaction import active_markdown_coordinator

    inputs, state = compiler_case
    snapshot = _snapshot(work, inputs)
    items = _consume(work, snapshot)
    item = next(items)
    calls = _semantic_backend(monkeypatch)
    coordinator = active_markdown_coordinator(work[0].vault, state)

    def interrupted(*_args):
        raise SystemExit('after durable draft before validation')

    with monkeypatch.context() as patch:
        patch.setattr(compile_pipeline_prompts.CompileAttempt, '_planned', interrupted)
        with pytest.raises(SystemExit):
            _resolve_saved(work, item, snapshot, state, coordinator, lambda: compile_pipeline_sources.iter_compile_records(()))
    assert calls == [True]

    def forbidden(*_args):
        raise AssertionError('semantic replay recaptured mutable pages')

    point_compile_path(monkeypatch, 'snapshot_compile_inputs', forbidden)
    result = _resolve_saved(work, item, snapshot, state, coordinator, forbidden)
    assert result.plan['operations'][0]['path'] == 'knowledge/notes/cedar-merge-order.md'
    assert len(calls) == 3
    items.close()
