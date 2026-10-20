"""Saved context uses the compiler's normal selection and identity checks."""
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_context_delta import _context, _records
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_refresh_selects_context_once import _planned
from tests.test_compile_streamed_archive_batches import _materialized


def test_saved_context_matches_existing_refresh_without_live_capture(monkeypatch):
    batch = _planned(monkeypatch)
    context = _context()
    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', lambda _paths: context)
    expected = compiler._refresh_compile_batch(batch)

    def forbidden(*_args):
        raise AssertionError('saved refresh reread live context')

    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', forbidden)
    actual = compiler.refresh_compile_batch_from_context(batch, context)
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
    joined = compiler._compile_batch_context_inputs(batch, context)
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
    from tests.test_compile_refresh_selects_context_once import _alter_refresh

    batch = _planned(monkeypatch)
    original = compiler.pack_compile_batches

    def altered(inputs, **kwargs):
        return (_alter_refresh(original(inputs, **kwargs)[0], field),)

    monkeypatch.setattr(compiler, 'pack_compile_batches', altered)
    with pytest.raises(ValueError, match='changed while refreshing context'):
        compiler.refresh_compile_batch_from_context(batch, _context())


def _semantic_backend(monkeypatch):
    import json

    import llm_client as client
    from context_budget import TokenUsage

    from tests.test_compile_critic_drop_cannot_complete_source_work import _proposed
    from tests.test_the_compile_decides_what_the_snapshot_already_knows import (
        _provider,
        _source_review,
    )

    provider = _provider()
    monkeypatch.setattr(compiler, '_compile_candidate_chain', lambda _batch: [provider])
    monkeypatch.setattr(compiler, 'probe_candidate', lambda _descriptor: True)
    replies = iter((json.dumps({'operations': [_proposed()]}),
        json.dumps({'reviews': [{'slug': 'cedar-merge-order', 'verdict': 'pass',
                                'reason': 'Supported by source.'}]}), _source_review()))
    calls = []

    def backend(*_args):
        calls.append(True)
        return client.BackendResponse(next(replies), TokenUsage(input_tokens=7, output_tokens=3))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    return calls


def _resolve_saved(work, item, snapshot, state, coordinator, capture):
    from compile_cache import CompileCache

    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot,
            dependencies=_dependencies(), capture=capture) as (ready, checkpoint):
        replay = controller.CompileReplay(work[0], work[1], checkpoint, dependencies=_dependencies())
        return compiler.resolve_compile_plan(ready.inputs, CompileCache(state), coordinator=coordinator,
                                              batch=ready, call_runner=replay)


def test_real_semantic_replay_uses_saved_context_and_draft_after_interruption(work, compiler_case, monkeypatch):
    from markdown_transaction import active_markdown_coordinator

    _, inputs, state = compiler_case
    snapshot = _snapshot(work, inputs)
    items = _consume(work, snapshot)
    item = next(items)
    calls = _semantic_backend(monkeypatch)
    coordinator = active_markdown_coordinator(work[0].vault, state)

    def interrupted(*_args):
        raise SystemExit('after durable draft before validation')

    with monkeypatch.context() as patch:
        patch.setattr(compiler._CompileAttempt, '_planned', interrupted)
        with pytest.raises(SystemExit):
            _resolve_saved(work, item, snapshot, state, coordinator, lambda: compiler.iter_compile_records(()))
    assert calls == [True]

    def forbidden(*_args):
        raise AssertionError('semantic replay recaptured mutable pages')

    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', forbidden)
    result = _resolve_saved(work, item, snapshot, state, coordinator, forbidden)
    assert result.plan['operations'][0]['path'] == 'knowledge/notes/cedar-merge-order.md'
    assert len(calls) == 3
    items.close()
