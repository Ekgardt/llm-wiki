"""One saved planned batch resolves, publishes and recovers without new model work."""
import time

import compile_controller as controller
import compile_memory as compiler
import pytest
from markdown_transaction import active_markdown_coordinator

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_saved_context_refresh import _semantic_backend


@pytest.fixture
def planned(work, compiler_case, monkeypatch):
    _, inputs, state = compiler_case
    snapshot = _snapshot(work, inputs)
    items = _consume(work, snapshot)
    item = next(items)
    coordinator = active_markdown_coordinator(work[0].vault, state)
    calls = _semantic_backend(monkeypatch)
    try:
        yield item, snapshot, coordinator, calls
    finally:
        items.close()


def _execute(work, planned, **options):
    item, snapshot, coordinator, _calls = planned
    return controller.execute_planned_compile_batch(work[0], work[1], item, snapshot,
        coordinator=coordinator, dependencies=_dependencies(), trigger='manual', **options)


def _forbidden(*args, **kwargs):
    raise AssertionError('recovery repeated completed semantic work')


def test_planned_batch_completes_and_recovery_skips_resolution(work, planned, monkeypatch):
    completed = _execute(work, planned)
    assert (completed.stage, completed.outcome) == ('published', 'completed')
    assert len(planned[3]) == 3
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    assert _execute(work, planned, capture=_forbidden) == completed
    assert work[0].get(work[1].id).state == 'leased'


def test_accepted_intent_before_interruption_resumes_without_resolution(work, planned, monkeypatch):
    def interrupted(*args, **kwargs):
        raise SystemExit('accepted plan before publication')

    with monkeypatch.context() as patch:
        patch.setattr(controller, 'execute_compile_publication', interrupted)
        with pytest.raises(SystemExit):
            _execute(work, planned)
    assert len(planned[3]) == 3
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    assert _execute(work, planned, capture=_forbidden).stage == 'published'


def test_interrupted_validation_reuses_saved_draft(work, planned, monkeypatch):
    def interrupted(*args, **kwargs):
        raise SystemExit('draft saved before validation')

    with monkeypatch.context() as patch:
        patch.setattr(compiler._CompileAttempt, '_planned', interrupted)
        with pytest.raises(SystemExit):
            _execute(work, planned)
    assert len(planned[3]) == 1
    assert _execute(work, planned, capture=_forbidden).stage == 'published'
    assert len(planned[3]) == 3


def test_repair_capacity_is_preserved_for_delivery_controller(work, planned, monkeypatch):
    failure = compiler._CompileRepairCapacityError('repair requires partition', ('review evidence',))

    def failed(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compiler, 'resolve_compile_plan', failed)
    with pytest.raises(compiler._CompileRepairCapacityError) as caught:
        _execute(work, planned)
    assert caught.value is failure
    assert planned[3] == []
    assert work[0].get(work[1].id).state == 'leased'


@pytest.mark.parametrize('options', [{'cancelled': lambda: True}, {'deadline': 0.0}])
def test_cancelled_or_expired_batch_never_calls_model(work, planned, options):
    assert time.monotonic() > 0
    with pytest.raises(TimeoutError):
        _execute(work, planned, **options)
    assert planned[3] == []


def test_planned_execution_reuses_caller_delivery(work, planned):
    with controller.CompileDelivery(work[0], work[1]) as delivery:
        assert _execute(work, planned, delivery=delivery).stage == 'published'
    assert work[0].get(work[1].id).state == 'leased'


def test_unknown_call_blocks_delivery_and_cannot_repeat_after_reclaim(work, planned, monkeypatch):
    import llm_client

    calls = []

    def timeout(*args):
        calls.append(True)
        raise TimeoutError('request may have run')

    monkeypatch.setitem(llm_client._BACKENDS, 'fake', timeout)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        with controller.CompileDelivery(work[0], work[1]) as delivery:
            _execute(work, planned, delivery=delivery)
    assert work[0].get(work[1].id).blocked_capability == 'provider_outcome_unknown'
    work[0].unblock(work[1].id)
    replacement = work[0].claim('resumed-compile')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _execute((work[0], replacement, work[2]), planned, capture=_forbidden)
    assert calls == [True]
    assert work[0].get(work[1].id).state == 'blocked'


def test_capacity_decision_resumes_without_resolving_parent_again(work, planned, monkeypatch):
    failure = compiler._CompileRepairCapacityError('partition needed', ())

    def capacity(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(compiler, 'resolve_compile_plan', capacity)
        with pytest.raises(compiler._CompileRepairCapacityError) as caught:
            _execute(work, planned)
        assert caught.value is failure
    current = work[0].get_compile_event(planned[0].work_id)
    assert 'compile_repair' in current.details
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    with pytest.raises(compiler._CompileRepairCapacityError) as recovered:
        _execute(work, planned, capture=_forbidden)
    assert str(recovered.value) == str(failure)
    assert recovered.value.feedback == failure.feedback
    assert planned[3] == []
