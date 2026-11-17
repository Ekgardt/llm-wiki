"""Provisional grouping cannot dispatch before its one fresh context selection."""
from dataclasses import replace
from types import SimpleNamespace

import compile_pipeline
import compile_receipts
import llm_client as client
import pytest
from llm_wiki import (
    compile_pipeline_apply,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from reliable_memory import sha256_bytes

from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path
from tests.compile_refresh_selects_context_once_helpers import _alter_refresh, _planned


def _observed_selection(monkeypatch):
    calls = []
    original = compile_pipeline_batches.fitting_context

    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, 'fitting_context', observed)
    return calls


def test_run_planning_does_not_select_unused_optional_context(monkeypatch):
    calls = _observed_selection(monkeypatch)
    batch = _planned(monkeypatch)
    assert calls == []
    assert batch.context_pending
    assert [source.logical_path for source in batch.inputs.sources] == [batch.inputs.dailies[0].logical_path]


def test_public_packing_still_returns_ready_complete_batches(monkeypatch):
    calls = _observed_selection(monkeypatch)
    batch = compile_pipeline_batches.pack_compile_batches(_inputs(optional=True), model=None)[0]
    assert len(calls) == 1
    assert not getattr(batch, 'context_pending', False)
    assert 'knowledge/notes/évidence.md' in {source.logical_path for source in batch.inputs.sources}


def _invoke_boundary(boundary, batch):
    calls = {
        'resolve': lambda: compile_pipeline_batches.resolve_compile_plan(batch.inputs, None, coordinator=None, batch=batch),
        'apply': lambda: compile_pipeline_apply.apply_compile_plan(batch.inputs, {'operations': []}, action_key='a' * 64,
                                                     trigger='manual', coordinator=None, batch=batch, provider_budget={}),
        'run': lambda: compile_pipeline.run_batch(batch, SimpleNamespace(dry_run=True), coordinator=None,
                                           deadline=float('inf'), cancelled=None, owner=None),
        'attempt': lambda: compile_pipeline_prompts.CompileAttempt(batch.inputs, None, batch, None),
    }
    return calls[boundary]()


@pytest.mark.parametrize('boundary', ['resolve', 'apply', 'run', 'attempt'])
def test_provisional_batches_refuse_before_cache_dispatch_or_publication(monkeypatch, boundary):
    batch = _planned(monkeypatch)
    with pytest.raises(ValueError, match='context.*refresh'):
        _invoke_boundary(boundary, batch)


def test_refresh_selects_fresh_context_once_and_matches_full_ready_packing(monkeypatch):
    batch = _planned(monkeypatch)
    context = _inputs(optional=True)
    note = context.sources[-1]
    fresh = replace(note, content=b'fresh context', sha256=sha256_bytes(b'fresh context'))
    context = replace(context, sources=(fresh,), targets=(compile_pipeline_sources.TargetSnapshot('knowledge/notes/current.md', b'new', sha256_bytes(b'new')),))
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', lambda paths: context)
    calls = _observed_selection(monkeypatch)
    ready = compile_pipeline_batches.refresh_compile_batch(batch)
    assert len(calls) == 1
    assert not ready.context_pending
    daily = tuple(compile_receipts.SourceSnapshot(part.logical_path, part.content, part.sha256) for part in batch.inputs.dailies)
    expected_inputs = compile_pipeline_sources.CompileInputs(batch.inputs.dailies, tuple(sorted((*daily, *context.sources), key=lambda source: source.logical_path)), context.targets, context.vault_files)
    expected = compile_pipeline_batches.pack_compile_batches(expected_inputs, model=batch.planning_model,
                                             budget=compile_pipeline_prompts.packing_budget(batch.packing, batch.planning_model),
                                             planning_candidates=batch.planning_candidates)[0]
    assert ready == expected
    assert compile_pipeline_prompts.draft_layout(ready.inputs) == compile_pipeline_prompts.draft_layout(expected.inputs)
    assert ready.manifest == batch.manifest
    assert ready.planning_candidates is batch.planning_candidates
    assert ready.inputs.targets == context.targets
    assert ready.inputs.sources[-1] == fresh


@pytest.mark.parametrize('field', ['manifest', 'model', 'candidates', 'budget'])
def test_refresh_refuses_any_planning_identity_drift(monkeypatch, field):
    batch = _planned(monkeypatch)
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', lambda paths: compile_pipeline_sources.CompileInputs((), (), ()))
    original = compile_pipeline_batches.pack_compile_batches

    def altered(inputs, **kwargs):
        ready = original(inputs, **kwargs)[0]
        return (_alter_refresh(ready, field),)

    point_compile_path(monkeypatch, 'pack_compile_batches', altered)
    with pytest.raises(ValueError, match='changed while refreshing context'):
        compile_pipeline_batches.refresh_compile_batch(batch)


@pytest.mark.parametrize('method', ['_call', '_fits'])
def test_low_level_dispatch_and_fit_cannot_use_a_pending_batch(monkeypatch, method):
    ready = compile_pipeline_batches.pack_compile_batches(_inputs(optional=True), model=None)[0]
    attempt = compile_pipeline_prompts.CompileAttempt(ready.inputs, None, ready, None)
    attempt.batch = _planned(monkeypatch)
    descriptor = client.provider_candidates('fake')[0]
    calls = {'_call': lambda: attempt._call(descriptor, 'prompt', 'system', {}),
             '_fits': lambda: attempt._fits('prompt', 'system', {}, descriptor)}
    with pytest.raises(ValueError, match='context.*refresh'):
        calls[method]()


def test_pending_marker_never_enters_persisted_packing(monkeypatch):
    batch = _planned(monkeypatch)
    assert 'context_pending' not in batch.packing.canonical()
