"""The streamed controller must use the compiler's authoritative run planning."""
import json
from dataclasses import replace
from types import SimpleNamespace

import compile_pipeline
import llm_client as client
import pytest
from context_budget import TokenCount
from model_dlp import DLPPolicy

from tests.compile_call_controller_helpers import work as work
from tests.compile_durable_stream_capture_helpers import _paths
from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_repair_completion_helpers import _coordinator
from tests.compile_saved_context_refresh_helpers import _semantic_backend
from tests.compile_shared_run_planning_helpers import _basis, _candidate
from tests.compile_streamed_archive_batches_helpers import _archive, _materialized
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import _provider


def _prepare(monkeypatch, candidates=None):
    candidates = tuple(candidates or (_candidate(),))
    point_compile_path(monkeypatch, 'provider_candidates', lambda *args, **kwargs: candidates)
    point_compile_path(monkeypatch, 'probe_candidate', lambda item: item.resolution_failure is None)
    return compile_pipeline.prepare_compile_planning(deadline=float('inf'))


def _forbidden(*args, **kwargs):
    raise AssertionError('planning replay rediscovered a provider or materialized the archive')


def test_streamed_planning_matches_eager_run_without_rediscovery(monkeypatch):
    prepared = _prepare(monkeypatch)
    expected, refused = compile_pipeline.pack_for_run(_inputs(), float('inf'))
    assert not refused
    point_compile_path(monkeypatch, 'provider_candidates', _forbidden)
    with _archive(_inputs()) as reader:
        monkeypatch.setattr(reader, 'materialize', _forbidden)
        monkeypatch.setattr(reader, 'planning_inputs', _forbidden)
        actual = tuple(_materialized(batch) for batch in prepared.batches(reader, _forbidden))
        assert actual == expected
        assert tuple(_materialized(batch) for batch in prepared.batches(reader, _forbidden)) == expected


def test_planning_keeps_failed_candidates_and_selects_first_ready_model(monkeypatch):
    first = replace(_candidate(), model='unavailable', _resolution_failure='provider_timeout')
    second = replace(_candidate(), model='ready', candidate_index=1)
    prepared = _prepare(monkeypatch, (first, second))
    assert prepared.model == 'ready'
    assert prepared.candidates == (first, second)


@pytest.mark.parametrize('change', [
    {'model': 'another-model'},
    {'inference_settings': {'reasoning': 'high'}},
    {'_endpoint': 'https://secret.example/token'},
    {'_resolution_failure': 'provider_timeout'},
    {'candidate_index': 3},
])
def test_request_identity_binds_candidate_behavior(monkeypatch, change):
    candidate = _candidate()
    original = _prepare(monkeypatch, (candidate,)).request()
    changed = _prepare(monkeypatch, (replace(candidate, **change),)).request()
    assert original != changed
    assert 'secret.example' not in json.dumps(changed)


@pytest.mark.parametrize('field,value', [
    ('planning_window', 131072), ('catalog_sha256', 'e' * 64),
    ('environment_sha256', 'f' * 64), ('cli_version', 'other-version'),
])
def test_request_binds_provider_planning_evidence(monkeypatch, field, value):
    candidate = _candidate()
    basis = _basis(candidate)
    original = _prepare(monkeypatch, (replace(candidate, _codex_basis=basis),)).request()
    changed = _prepare(monkeypatch, (replace(candidate, _codex_basis=replace(basis, **{field: value})),)).request()
    assert original != changed
    assert '/private/' not in json.dumps(original)


def test_request_binds_executable_bytes_and_candidate_order(monkeypatch):
    candidate = _candidate()
    basis = _basis(candidate)
    owned = replace(candidate, _codex_basis=basis)
    changed = replace(owned, _codex_basis=replace(basis, executable=replace(basis.executable, sha256='f' * 64)))
    assert _prepare(monkeypatch, (owned,)).request() != _prepare(monkeypatch, (changed,)).request()
    fallback = replace(candidate, model='fallback')
    assert _prepare(monkeypatch, (owned, fallback)).request() != _prepare(monkeypatch, (fallback, owned)).request()


def test_expired_preparation_does_not_discover_providers(monkeypatch):
    point_compile_path(monkeypatch, 'provider_candidates', _forbidden)
    with pytest.raises(TimeoutError):
        compile_pipeline.prepare_compile_planning(deadline=0)


def test_protected_codex_request_accepts_real_tuple_evidence_and_binds_it():
    candidate = _candidate()
    basis = _basis(candidate)
    prepared = client.PreparedCodex(basis.executable, 'payload', candidate.model, 'medium', basis)
    transport = SimpleNamespace(system_prompt='system', prompt='prompt', schema=None,
                                policy=DLPPolicy(), codex=prepared)
    original = client.protected_call_digest(candidate, transport, 'text', TokenCount())
    assert len(original) == 64
    changed = replace(basis, config_files=(('/private/config', 'e' * 64),))
    transport.codex = replace(prepared, basis=changed)
    assert client.protected_call_digest(candidate, transport, 'text', TokenCount()) != original
    transport.codex = replace(prepared, executable=replace(basis.executable, identity=(1, 3)))
    assert client.protected_call_digest(candidate, transport, 'text', TokenCount()) != original


def test_shared_planning_drives_admitted_capture_through_queue_completion(
    work, compiler_case, monkeypatch,
):
    import compile_controller as controller
    import compile_progress as progress

    planning = _prepare(monkeypatch, (_provider(),))
    preparation = controller.CompileCapturePreparation(_paths(compiler_case), _dependencies(), planning.request())
    snapshot = controller.admit_compile_capture(work[0], work[1], lambda: preparation)
    calls = _semantic_backend(monkeypatch)
    options = dict(coordinator=_coordinator(work), dependencies=preparation.dependencies,
                   request=planning.request(), plan=planning.batches, trigger='manual')
    changed = {**options, 'request': replace(planning, model='another-model').request()}
    with pytest.raises(progress.ProgressConflict, match='preflight checkpoint identity changed'):
        controller.execute_compile_work(work[0], work[1], snapshot, **changed)
    assert calls == []
    assert controller.execute_compile_work(work[0], work[1], snapshot, **options).stage == 'published'
    assert len(calls) == 3
    point_compile_path(monkeypatch, 'provider_candidates', _forbidden)
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert controller.execute_compile_work(work[0], work[1], snapshot, **options).stage == 'published'
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'completed')
    assert work[0].acknowledge(work[1]).state == 'succeeded'
