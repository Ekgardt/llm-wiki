"""Provisional grouping cannot dispatch before its one fresh context selection."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as compiler
import llm_client as client
import pytest
from reliable_memory import sha256_bytes

from tests.test_compile_packing_counts_original_entry_metadata import _inputs


def _configure_run(monkeypatch):
    candidates = client.provider_candidates('fake', max_tokens=4000)
    monkeypatch.setattr(compiler, 'provider_candidates', lambda *args, **kwargs: candidates)
    monkeypatch.setattr(compiler, 'probe_candidate', lambda candidate: True)
    return candidates


def _observed_selection(monkeypatch):
    calls = []
    original = compiler._fitting_context

    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, '_fitting_context', observed)
    return calls


def _planned(monkeypatch):
    _configure_run(monkeypatch)
    batches, refused = compiler._pack_for_run(_inputs(optional=True), float('inf'))
    assert not refused
    return batches[0]


def test_run_planning_does_not_select_unused_optional_context(monkeypatch):
    calls = _observed_selection(monkeypatch)
    batch = _planned(monkeypatch)
    assert calls == []
    assert batch.context_pending
    assert [source.logical_path for source in batch.inputs.sources] == [batch.inputs.dailies[0].logical_path]


def test_public_packing_still_returns_ready_complete_batches(monkeypatch):
    calls = _observed_selection(monkeypatch)
    batch = compiler.pack_compile_batches(_inputs(optional=True), model=None)[0]
    assert len(calls) == 1
    assert not getattr(batch, 'context_pending', False)
    assert 'knowledge/notes/évidence.md' in {source.logical_path for source in batch.inputs.sources}


def _invoke_boundary(boundary, batch):
    calls = {
        'resolve': lambda: compiler.resolve_compile_plan(batch.inputs, None, coordinator=None, batch=batch),
        'apply': lambda: compiler.apply_compile_plan(batch.inputs, {'operations': []}, action_key='a' * 64,
                                                     trigger='manual', coordinator=None, batch=batch, provider_budget={}),
        'run': lambda: compiler._run_batch(batch, SimpleNamespace(dry_run=True), coordinator=None,
                                           deadline=float('inf'), cancelled=None, owner=None),
        'attempt': lambda: compiler._CompileAttempt(batch.inputs, None, batch, None),
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
    context = replace(context, sources=(fresh,), targets=(compiler.TargetSnapshot('knowledge/notes/current.md', b'new', sha256_bytes(b'new')),))
    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', lambda paths: context)
    calls = _observed_selection(monkeypatch)
    ready = compiler._refresh_compile_batch(batch)
    assert len(calls) == 1
    assert not ready.context_pending
    daily = tuple(compiler.SourceSnapshot(part.logical_path, part.content, part.sha256) for part in batch.inputs.dailies)
    expected_inputs = compiler.CompileInputs(batch.inputs.dailies, tuple(sorted((*daily, *context.sources), key=lambda source: source.logical_path)), context.targets, context.vault_files)
    expected = compiler.pack_compile_batches(expected_inputs, model=batch.planning_model,
                                             budget=compiler._packing_budget(batch.packing, batch.planning_model),
                                             planning_candidates=batch.planning_candidates)[0]
    assert ready == expected
    assert compiler._draft_layout(ready.inputs) == compiler._draft_layout(expected.inputs)
    assert ready.manifest == batch.manifest
    assert ready.planning_candidates is batch.planning_candidates
    assert ready.inputs.targets == context.targets
    assert ready.inputs.sources[-1] == fresh


def _alter_refresh(batch, field):
    changes = {
        'manifest': lambda: replace(batch, manifest=()),
        'model': lambda: replace(batch, planning_model='another-model'),
        'candidates': lambda: replace(batch, planning_candidates=()),
        'budget': lambda: replace(batch, packing=replace(batch.packing, max_input_tokens=batch.packing.max_input_tokens + 1)),
    }
    return changes[field]()


@pytest.mark.parametrize('field', ['manifest', 'model', 'candidates', 'budget'])
def test_refresh_refuses_any_planning_identity_drift(monkeypatch, field):
    batch = _planned(monkeypatch)
    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', lambda paths: compiler.CompileInputs((), (), ()))
    original = compiler.pack_compile_batches

    def altered(inputs, **kwargs):
        ready = original(inputs, **kwargs)[0]
        return (_alter_refresh(ready, field),)

    monkeypatch.setattr(compiler, 'pack_compile_batches', altered)
    with pytest.raises(ValueError, match='changed while refreshing context'):
        compiler._refresh_compile_batch(batch)


@pytest.mark.parametrize('method', ['_call', '_fits'])
def test_low_level_dispatch_and_fit_cannot_use_a_pending_batch(monkeypatch, method):
    ready = compiler.pack_compile_batches(_inputs(optional=True), model=None)[0]
    attempt = compiler._CompileAttempt(ready.inputs, None, ready, None)
    attempt.batch = _planned(monkeypatch)
    descriptor = client.provider_candidates('fake')[0]
    calls = {'_call': lambda: attempt._call(descriptor, 'prompt', 'system', {}),
             '_fits': lambda: attempt._fits('prompt', 'system', {}, descriptor)}
    with pytest.raises(ValueError, match='context.*refresh'):
        calls[method]()


def test_pending_marker_never_enters_persisted_packing(monkeypatch):
    batch = _planned(monkeypatch)
    assert 'context_pending' not in batch.packing.canonical()
