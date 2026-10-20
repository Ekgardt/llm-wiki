"""Retained work must not discover a new model/catalog during recovery."""
import json
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_atomic_capture import _interrupt_snapshot
from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _run
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_shared_run_planning import _basis, _candidate
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider


def _discovery_down(*args, **kwargs):
    raise OSError('provider discovery is unavailable')


def test_completed_leased_work_recovers_without_discovery(work, runtime, monkeypatch):
    assert _run(work)
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_captured_work_recovers_without_discovery(work, runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(controller, 'execute_compile_work', _discovery_down)
        with pytest.raises(OSError, match='discovery'):
            _run(work)
    assert runtime == []
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_orphan_capture_recovers_without_discovery(work, runtime, monkeypatch):
    with monkeypatch.context() as patch:
        _interrupt_snapshot(work, patch)
        with pytest.raises(OSError, match='snapshot event interrupted'):
            _run(work)
    assert runtime == []
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_saved_planning_does_not_bypass_live_dependency_verification(work, runtime, monkeypatch):
    assert _run(work)
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    monkeypatch.setenv('COMPILE_TEST_DEPENDENCY_CHANGE', 'changed')
    with pytest.raises(controller.progress.ProgressConflict, match='identity changed'):
        _run(work)
    assert len(runtime) == 3


def _interrupt_publication(*args, **kwargs):
    raise OSError('publication journal interrupted')


def test_partial_answers_recover_while_probe_is_down(work, runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(controller, 'save_compile_publication', _interrupt_publication)
        with pytest.raises(OSError, match='publication journal'):
            _run(work)
    assert len(runtime) == 3
    monkeypatch.setattr(compiler, 'probe_candidate', _discovery_down)
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_first_answer_replays_before_remaining_calls_without_reprobing(work, runtime, monkeypatch):
    original = controller.run_candidate_call

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('stopped after saved answer')

    with monkeypatch.context() as patch:
        patch.setattr(controller, 'run_candidate_call', interrupted)
        with pytest.raises(OSError, match='saved answer'):
            _run(work)
    assert len(runtime) == 1
    monkeypatch.setattr(compiler, 'probe_candidate', _discovery_down)
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_saved_fallback_order_survives_changed_readiness(work, runtime, monkeypatch):
    first = replace(_provider(), model='initially-down')
    second = replace(_provider(), candidate_index=1)
    monkeypatch.setattr(compiler, 'provider_candidates', lambda *a, **kw: [first, second])
    monkeypatch.setattr(compiler, 'probe_candidate', lambda item: item.model != first.model)
    with monkeypatch.context() as patch:
        patch.setattr(controller, 'save_compile_publication', _interrupt_publication)
        with pytest.raises(OSError, match='publication journal'):
            _run(work)
    assert len(runtime) == 3
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    monkeypatch.setattr(compiler, 'probe_candidate', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_all_unavailable_providers_can_be_retried_after_capture(work, runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(compiler, 'probe_candidate', lambda _: False)
        assert not _run(work)
    assert runtime == []
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _discovery_down)
    assert _run(work)
    assert len(runtime) == 3


def test_failed_discovery_admits_no_permanently_failed_plan(work, runtime, monkeypatch):
    candidate = replace(_provider(), _resolution_failure='provider_timeout')
    with monkeypatch.context() as patch:
        patch.setattr(compiler, 'provider_candidates', lambda *a, **kw: [candidate])
        with pytest.raises(compiler.CompilePlanUnavailable, match='planning evidence'):
            _run(work)
    origin = work[0].compile_origin(work[1])
    assert work[0].get_compile_event(controller.progress.root_work_id(origin)) is None
    assert runtime == []
    assert _run(work)
    assert len(runtime) == 3


def _retained_codex(monkeypatch):
    original = replace(_candidate(), provider='codex', _endpoint='https://private.example')
    basis = _basis(original)
    planning = compiler.CompileRunPlanning(basis.model, (basis.descriptor,))
    monkeypatch.setattr(compiler, 'provider_candidates', lambda *a, **kw: [original])
    monkeypatch.setattr(compiler, 'probe_candidate', _discovery_down)
    monkeypatch.setattr(compiler, 'resolve_codex_planning_basis', _discovery_down)
    return original, planning


def test_planning_round_trip_preserves_codex_evidence_without_endpoint(monkeypatch):
    _, planning = _retained_codex(monkeypatch)
    document = json.loads(json.dumps(planning.retained()))
    assert 'private.example' not in json.dumps(document)
    restored = compiler.restore_compile_planning(document)
    assert restored.request() == planning.request()
    assert restored.candidates[0]._codex_basis == planning.candidates[0]._codex_basis


@pytest.mark.parametrize('change', [
    {'model': 'another-model'}, {'_endpoint': 'https://different.example'},
    {'inference_settings': {'reasoning': 'high'}},
])
def test_retained_planning_rejects_changed_local_configuration(monkeypatch, change):
    original, planning = _retained_codex(monkeypatch)
    monkeypatch.setattr(compiler, 'provider_candidates',
                        lambda *a, **kw: [replace(original, **change)])
    with pytest.raises(ValueError, match='planning'):
        compiler.restore_compile_planning(planning.retained())


def test_retained_planning_checks_deadline_before_local_configuration(monkeypatch):
    _, planning = _retained_codex(monkeypatch)
    monkeypatch.setattr(compiler, 'provider_candidates', _discovery_down)
    with pytest.raises(TimeoutError):
        compiler.restore_compile_planning(planning.retained(), deadline=0)


@pytest.mark.parametrize('change', [
    {'schema': 'unsupported'}, {'candidates': []}, {'model': []}, {'extra': True},
])
def test_retained_planning_refuses_malformed_evidence(monkeypatch, change):
    _, planning = _retained_codex(monkeypatch)
    document = {**planning.retained(), **change}
    with pytest.raises(ValueError, match='planning'):
        compiler.restore_compile_planning(document)


def test_codex_implicit_model_restores_original_descriptor(monkeypatch):
    original = replace(_candidate(), provider='codex', model=None)
    basis = replace(_basis(original), model='resolved-model')
    planning = compiler.CompileRunPlanning(basis.model, (basis.descriptor,))
    monkeypatch.setattr(compiler, 'provider_candidates', lambda *a, **kw: [original])
    restored = compiler.restore_compile_planning(planning.retained())
    assert restored.request() == planning.request()
    assert restored.candidates[0]._codex_basis.original_descriptor.model is None
