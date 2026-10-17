"""A fresh batch must not reuse pre-reranker process environment evidence."""
from dataclasses import replace

import compile_memory as compiler
import llm_client as client
import pytest

from tests.test_codex_planning_basis_owns_the_selected_model import _basis
from tests.test_compile_packing_counts_original_entry_metadata import _inputs


def _empty_context(paths):
    return compiler.CompileInputs((), (), ())


def _batch(monkeypatch, tmp_path):
    selected = _basis(tmp_path)
    batch = compiler.pack_compile_batches(_inputs(), model=selected.model,
                                           planning_candidates=(selected.descriptor,))[0]
    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', _empty_context)
    return selected, batch


def test_refresh_requalifies_environment_before_next_batch(monkeypatch, tmp_path):
    selected, batch = _batch(monkeypatch, tmp_path)
    calls = []

    def resolve(candidate, *, deadline):
        calls.append((candidate, deadline))
        assert candidate == selected.original_descriptor
        return replace(selected, environment_sha256=client._codex_basis_digest(client.provider_environment()))

    monkeypatch.setattr(compiler, 'resolve_codex_planning_basis', resolve)
    monkeypatch.setenv('KMP_DUPLICATE_LIB_OK', 'changed-by-local-runtime')
    with pytest.raises(RuntimeError, match='environment'):
        client._prepare_codex(selected.descriptor, 'source', 'system')
    refreshed = compiler._refresh_compile_batch(batch)
    actual = refreshed.planning_candidates[0]
    client._prepare_codex(actual, 'source', 'system')
    assert len(calls) == 1
    assert actual._codex_basis is not selected
    assert actual.model == selected.model
    assert refreshed.manifest == batch.manifest
    assert refreshed.inputs.dailies == batch.inputs.dailies
    with pytest.raises(RuntimeError, match='environment'):
        client._prepare_codex(selected.descriptor, 'source', 'system')


def test_valid_owned_basis_is_kept_without_new_resolution(monkeypatch, tmp_path):
    selected, batch = _batch(monkeypatch, tmp_path)

    def refuse_new_resolution(*args, **kwargs):
        raise AssertionError('unchanged environment does not need requalification')

    monkeypatch.setattr(compiler, 'resolve_codex_planning_basis', refuse_new_resolution)
    refreshed = compiler._refresh_compile_batch(batch)
    assert refreshed.planning_candidates[0] is batch.planning_candidates[0]
    client._prepare_codex(refreshed.planning_candidates[0], 'source', 'system')


def test_changed_model_is_refused_by_refresh(monkeypatch, tmp_path):
    selected, batch = _batch(monkeypatch, tmp_path)

    def changed_model(candidate, *, deadline):
        return replace(selected, model='other-model', environment_sha256=client._codex_basis_digest(client.provider_environment()))

    monkeypatch.setattr(compiler, 'resolve_codex_planning_basis', changed_model)
    monkeypatch.setenv('KMP_INIT_AT_FORK', 'changed-by-local-runtime')
    with pytest.raises(ValueError, match='provider|model|settings'):
        compiler._refresh_compile_batch(batch)


def test_requalification_failure_is_not_hidden(monkeypatch, tmp_path):
    selected, batch = _batch(monkeypatch, tmp_path)

    def unavailable(candidate, *, deadline):
        raise RuntimeError('native metadata unavailable')

    monkeypatch.setattr(compiler, 'resolve_codex_planning_basis', unavailable)
    monkeypatch.setenv('TORCHINDUCTOR_CACHE_DIR', str(tmp_path / 'new-local-cache'))
    with pytest.raises(ValueError, match='revalidation|requalification'):
        compiler._refresh_compile_batch(batch)
