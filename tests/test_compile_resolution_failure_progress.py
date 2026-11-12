"""An exhausted validated-plan attempt must not starve independent source work."""
import json
from types import SimpleNamespace

import compile_controller as controller
import llm_client
import pytest
from llm_wiki import compile_pipeline_batches
from queue_core import (
    CompileCompletionError,
)

from tests.compile_call_controller_helpers import work as work
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies, _snapshot
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_repair_child_helpers import _child_backend
from tests.compile_repair_completion_helpers import _coordinator
from tests.compile_tree_execution_helpers import _execute_planned
from tests.compile_tree_execution_helpers import saved_tree as saved_tree
from tests.compile_work_completion_helpers import _plan
from tests.repair_capacity_repartitions_complete_source_work_helpers import _batch


def _invalid_first_child(monkeypatch, saved_tree):
    original = compile_pipeline_batches.resolve_compile_plan
    calls = []
    first = saved_tree[3][0]

    def invalid(*args):
        calls.append(True)
        return llm_client.BackendResponse('not a JSON plan')

    def resolve(*args, **kwargs):
        child = SimpleNamespace(feedback=kwargs['critic_feedback'])
        _child_backend(monkeypatch, child)
        if child.feedback == first.feedback:
            monkeypatch.setitem(llm_client.BACKENDS, 'fake', invalid)
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, 'resolve_compile_plan', resolve)
    return calls


def test_invalid_first_child_keeps_failure_and_finishes_sibling_without_repeating_calls(
    work, compiler_case, saved_tree, monkeypatch,
):
    calls = _invalid_first_child(monkeypatch, saved_tree)
    assert _execute_planned(work, saved_tree) is None
    first, second = saved_tree[3]
    assert work[0].get_compile_event(second.work_id).stage == 'published'
    expected = {(part.logical_path, part.sha256) for part in first.batch.inputs.dailies}
    assert expected <= set(work[0].source_failure_keys())
    assert work[0].get(work[1].id).state == 'leased'
    count = len(calls)
    assert count > 0
    assert _execute_planned(work, saved_tree) is None
    assert len(calls) == count
    assert work[0].get_compile_event(saved_tree[2].work_id) == saved_tree[2]


def test_failure_diagnostic_write_error_stops_before_sibling(
    work, compiler_case, saved_tree, monkeypatch,
):
    _invalid_first_child(monkeypatch, saved_tree)

    def unavailable(*args, **kwargs):
        raise OSError('failure diagnostic storage unavailable')

    point_compile_path(monkeypatch, 'record_compile_source_failures', unavailable)
    with pytest.raises(OSError, match='failure diagnostic storage'):
        _execute_planned(work, saved_tree)
    assert work[0].get_compile_event(saved_tree[3][1].work_id) is None


def test_only_exhausted_resolution_has_specific_failure_type():
    attempt = SimpleNamespace(lineage=('draft:fake:validation_error',), critic_feedback=())
    failure = compile_pipeline_batches.unresolved_compile_error(attempt)
    assert isinstance(failure, compile_pipeline_batches.CompilePlanUnavailable)
    assert isinstance(failure, RuntimeError)
    assert 'validation_error' in str(failure)


def test_capacity_failure_keeps_existing_repartition_contract():
    attempt = SimpleNamespace(lineage=('draft:fake:input_budget',), critic_feedback=('complete proposal',))
    failure = compile_pipeline_batches.unresolved_compile_error(attempt)
    assert isinstance(failure, compile_pipeline_batches.CompileRepairCapacityError)
    assert failure.feedback == attempt.critic_feedback


def _root_resolver(monkeypatch, feedback):
    original = compile_pipeline_batches.resolve_compile_plan
    operations = [item for record in feedback for item in json.loads(record)['operations']]
    first_day = min(item['evidence'][0]['daily_date'] for item in operations)
    calls = []

    def invalid(*args):
        calls.append(True)
        return llm_client.BackendResponse('invalid JSON')

    def resolve(*args, **kwargs):
        dates = {part.logical_path.rsplit('/', 1)[1][:-3] for part in kwargs['batch'].inputs.dailies}
        selected = [item for item in operations if item['evidence'][0]['daily_date'] in dates]
        _child_backend(monkeypatch, SimpleNamespace(feedback=(json.dumps({'operations': selected}),)))
        if first_day in dates:
            monkeypatch.setitem(llm_client.BACKENDS, 'fake', invalid)
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, 'resolve_compile_plan', resolve)
    return calls


def test_failed_first_root_allows_later_root_but_never_whole_work_completion(
    work, compiler_case, monkeypatch,
):
    batch, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, batch.inputs)
    calls = _root_resolver(monkeypatch, feedback)
    options = dict(coordinator=_coordinator(work), dependencies=_dependencies(),
                   request={'model': MODEL}, plan=_plan, trigger='manual')
    assert controller.execute_compile_work(work[0], work[1], snapshot, **options) is None
    plans = list(controller.iter_compile_preflight(work[0], work[1], snapshot,
        dependencies=_dependencies(), request={'model': MODEL}, plan=_plan, on_refused=None))
    assert len(plans) == 2
    assert work[0].get_compile_event(plans[1].work_id).stage == 'published'
    with pytest.raises(CompileCompletionError, match='compile_work_incomplete'):
        work[0].publish_result(work[1], operation_id=work[1].id, result=b'claimed success')
    count = len(calls)
    assert count > 0
    assert controller.execute_compile_work(work[0], work[1], snapshot, **options) is None
    assert len(calls) == count
