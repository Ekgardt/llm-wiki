"""Saved partitions execute independently without masking incomplete delivery."""
from types import SimpleNamespace

import compile_controller as controller
import compile_memory as compiler
import llm_client
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_repair_checkpoint import _read, _save
from tests.test_compile_repair_checkpoint import repair as repair
from tests.test_compile_repair_child import _child_backend
from tests.test_compile_repair_completion import _coordinator
from tests.test_repair_capacity_repartitions_complete_source_work import _batch


def _execute(work, repair, **options):
    return controller.execute_compile_tree(work[0], work[1], repair[1], repair[0],
        coordinator=_coordinator(work), dependencies=_dependencies(), trigger='manual', **options)


def _backend_per_leaf(monkeypatch):
    original = compiler.resolve_compile_plan
    calls = []

    def resolve(*args, **kwargs):
        child = SimpleNamespace(feedback=kwargs['critic_feedback'])
        calls.append(_child_backend(monkeypatch, child))
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, 'resolve_compile_plan', resolve)
    return calls


def _forbidden(*args, **kwargs):
    raise AssertionError('completed tree called the model again')


@pytest.fixture
def saved_tree(work, compiler_case):
    original, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, original.inputs)
    items = _consume(work, snapshot)
    try:
        planned = next(items)
        with controller.open_prepared_compile_batch(work[0], work[1], planned, snapshot,
                dependencies=_dependencies()) as (batch, context):
            repair = batch, context, compiler._CompileRepairCapacityError('capacity', feedback)
            saved = _save(work, repair)
            children = _read(work, repair, saved)
        yield planned, snapshot, saved, children
    finally:
        items.close()


def _execute_planned(work, saved_tree, **options):
    return controller.execute_planned_compile_tree(work[0], work[1], saved_tree[0], saved_tree[1],
        coordinator=_coordinator(work), dependencies=_dependencies(), trigger='manual', **options)


def test_tree_executes_saved_partition_and_resumes_without_model(work, repair, monkeypatch):
    saved = _save(work, repair)
    calls = _backend_per_leaf(monkeypatch)
    completed = _execute(work, repair)
    assert completed.stage == 'published'
    assert [len(leaf) for leaf in calls] == [3, 3]
    assert completed.details['repair_completion']['repair_digest'] == controller.progress._event_digest(saved)
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    assert _execute(work, repair) == completed
    assert work[0].get(work[1].id).state == 'leased'


def test_tree_executes_nested_saved_partition(work, repair, monkeypatch):
    saved = _save(work, repair)
    branch = _read(work, repair, saved)[1]
    context = controller.begin_compile_repair_batch(work[0], work[1], branch, repair[1], repair[0],
        dependencies=_dependencies())
    failure = compiler._CompileRepairCapacityError('nested capacity', branch.feedback)
    controller.save_compile_repair(work[0], work[1], context, branch.batch, failure,
        dependencies=_dependencies())
    calls = _backend_per_leaf(monkeypatch)
    assert _execute(work, repair).stage == 'published'
    assert [len(leaf) for leaf in calls] == [3, 3, 3]
    assert work[0].get_compile_event(branch.work_id).stage == 'published'


def test_uncertain_child_does_not_stop_sibling_or_become_success(work, saved_tree, monkeypatch):
    saved = saved_tree[2]
    first, second = saved_tree[3]
    original = compiler.resolve_compile_plan
    calls = []

    def timeout(*args):
        calls.append('unknown')
        raise TimeoutError('provider may have run')

    def resolve(*args, **kwargs):
        child = SimpleNamespace(feedback=kwargs['critic_feedback'])
        _child_backend(monkeypatch, child)
        if child.feedback == first.feedback:
            monkeypatch.setitem(llm_client._BACKENDS, 'fake', timeout)
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, 'resolve_compile_plan', resolve)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _execute_planned(work, saved_tree)
    assert work[0].get_compile_event(second.work_id).stage == 'published'
    assert work[0].get_compile_event(first.work_id).outcome == 'running'
    assert work[0].get_compile_event(saved.work_id) == saved
    assert work[0].get(work[1].id).blocked_capability == 'provider_outcome_unknown'
    work[0].unblock(work[1].id)
    replacement = work[0].claim('tree-recovery')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _execute_planned((work[0], replacement, work[2]), saved_tree, capture=_forbidden)
    assert calls == ['unknown']


def test_planned_tree_owns_archive_and_accepts_existing_delivery(work, saved_tree, monkeypatch):
    calls = _backend_per_leaf(monkeypatch)
    with controller.CompileDelivery(work[0], work[1]) as delivery:
        completed = _execute_planned(work, saved_tree, delivery=delivery, capture=_forbidden)
    assert completed.stage == 'published'
    assert [len(leaf) for leaf in calls] == [3, 3]
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    assert _execute_planned(work, saved_tree, capture=_forbidden) == completed


def test_new_capacity_failure_is_saved_and_executed_in_same_tree(work, repair, monkeypatch):
    original = compiler.resolve_compile_plan
    feedback_seen = []

    def resolve(*args, **kwargs):
        feedback = kwargs['critic_feedback']
        feedback_seen.append(feedback)
        if not feedback:
            raise repair[2]
        _child_backend(monkeypatch, SimpleNamespace(feedback=feedback))
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, 'resolve_compile_plan', resolve)
    assert _execute(work, repair).stage == 'published'
    assert len(feedback_seen) == 3
    assert feedback_seen[0] == ()


def test_indivisible_child_does_not_hide_or_stop_other_sources(work, repair, monkeypatch):
    saved = _save(work, repair)
    first, second = _read(work, repair, saved)
    original = compiler.resolve_compile_plan

    def resolve(*args, **kwargs):
        child = SimpleNamespace(feedback=kwargs['critic_feedback'])
        if child.feedback == first.feedback:
            raise compiler._CompileRepairCapacityError('indivisible', first.feedback)
        _child_backend(monkeypatch, child)
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, 'resolve_compile_plan', resolve)
    assert _execute(work, repair) is None
    assert 'compile_repair' in work[0].get_compile_event(first.work_id).details
    assert work[0].get_compile_event(second.work_id).stage == 'published'
    assert work[0].get_compile_event(saved.work_id) == saved
    assert work[0].get(work[1].id).state == 'leased'


@pytest.mark.parametrize('failure', [
    OSError('storage unavailable'),
    controller.progress.ProgressConflict('authority changed'),
    RuntimeError('semantic rejection'),
])
def test_unsafe_failure_stops_tree_before_admitting_siblings(work, repair, monkeypatch, failure):
    saved = _save(work, repair)
    children = _read(work, repair, saved)

    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compiler, 'resolve_compile_plan', unavailable)
    with pytest.raises(type(failure), match=str(failure)):
        _execute(work, repair)
    assert work[0].get_compile_event(children[1].work_id) is None


@pytest.mark.parametrize('options', [{'deadline': 0}, {'cancelled': lambda: True}])
def test_tree_checks_cancellation_before_child_admission(work, repair, options):
    saved = _save(work, repair)
    children = _read(work, repair, saved)
    with pytest.raises(TimeoutError):
        _execute(work, repair, **options)
    for child in children:
        assert work[0].get_compile_event(child.work_id) is None
