"""A whole compile needs complete source coverage and freshly verified receipts."""
from contextlib import contextmanager

import compile_controller as controller
import llm_client
import pytest
from llm_wiki import compile_pipeline_receipts, compile_pipeline_sources

from tests.compile_call_controller_helpers import work as work
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies, _snapshot
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_repair_child_helpers import _child_backend
from tests.compile_repair_completion_helpers import _coordinator
from tests.compile_work_completion_helpers import (
    _confirm,
    _execute,
    _plan,
    _refuse_first,
    multi,  # noqa: F401
)


def _forbidden(*args, **kwargs):
    raise AssertionError('confirmation or recovery repeated model/capture work')


def test_unexecuted_plan_has_no_completion_or_child_admission(work, multi, monkeypatch):  # noqa: F811
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert _confirm(work, multi) is None
    items = controller.iter_compile_preflight(work[0], work[1], multi[0],
        dependencies=_dependencies(), request={'model': MODEL}, plan=_plan, on_refused=_forbidden)
    for item in items:
        assert work[0].get_compile_event(item.work_id) is None
    assert work[0].get_compile_event(multi[0].work_id).stage == 'pending'


def test_complete_work_binds_all_roots_and_revalidates_receipts(work, multi, monkeypatch):  # noqa: F811
    completed = _execute(work, multi)
    assert completed.details['work_completion']['batch_count'] == 2
    assert [len(calls) for calls in multi[1]] == [3, 3]
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert _execute(work, multi, capture=_forbidden) == completed
    assert _confirm(work, multi) == completed
    assert work[0].get(work[1].id).state == 'leased'
    next((work[0].vault / 'knowledge/daily/receipts').glob('v4-*.md')).unlink()
    with pytest.raises(ValueError, match='receipt'):
        _confirm(work, multi)


def _refuse_all(reader, refused):
    for batch in _plan(reader, refused):
        refused(batch.inputs)
    return iter(())


@pytest.mark.parametrize('plan,call_counts', [(_refuse_first, [3]), (_refuse_all, [])])
def test_refused_sources_prevent_completion_even_when_every_executed_batch_succeeds(work, multi, plan, call_counts):  # noqa: F811
    assert _execute(work, multi, plan=plan) is None
    assert [len(calls) for calls in multi[1]] == call_counts
    assert work[0].get_compile_event(multi[0].work_id).stage == 'pending'
    assert work[0].get(work[1].id).state == 'leased'


def test_final_event_failure_recovers_without_model_calls(work, multi, monkeypatch):  # noqa: F811
    original = work[0].record_compile_event

    def interrupted(lease, event):
        if 'work_completion' in event.details:
            raise OSError('whole-work journal unavailable')
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError, match='whole-work journal'):
            _execute(work, multi)
    assert [len(calls) for calls in multi[1]] == [3, 3]
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert _execute(work, multi, capture=_forbidden).stage == 'published'


def test_entire_confirmation_uses_one_writer_gate(work, multi, monkeypatch):  # noqa: F811
    _execute(work, multi)
    original = compile_pipeline_receipts.compile_writer_gate
    entered = []

    @contextmanager
    def counted(*args, **kwargs):
        entered.append(True)
        with original(*args, **kwargs):
            yield

    point_compile_path(monkeypatch, 'compile_writer_gate', counted)
    assert _confirm(work, multi).stage == 'published'
    assert entered == [True]


@pytest.mark.parametrize('options', [{'deadline': 0}, {'cancelled': lambda: True}])
def test_work_cancellation_prevents_execution(work, multi, options):  # noqa: F811
    with pytest.raises(TimeoutError):
        _execute(work, multi, **options)
    assert multi[1] == []


def test_empty_source_manifest_completes_without_model_calls(work, compiler_case, monkeypatch):
    snapshot = _snapshot(work, compile_pipeline_sources.CompileInputs((), (), ()))
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    completed = _execute(work, (snapshot, []))
    assert completed.details['work_completion']['batch_count'] == 0
    assert _confirm(work, (snapshot, [])) == completed


def test_one_completed_root_cannot_complete_remaining_sources(work, multi):  # noqa: F811
    from contextlib import closing

    items = controller.iter_compile_preflight(work[0], work[1], multi[0],
        dependencies=_dependencies(), request={'model': MODEL}, plan=_plan, on_refused=_forbidden)
    with closing(items):
        item = next(items)
        assert controller.execute_planned_compile_tree(work[0], work[1], item, multi[0],
            coordinator=_coordinator(work), dependencies=_dependencies(), trigger='manual').stage == 'published'
    assert _confirm(work, multi) is None
    assert [len(calls) for calls in multi[1]] == [3]


def _uncertain_first_backend(monkeypatch, unknown_calls):
    original = _child_backend

    def timeout(*args):
        unknown_calls.append(True)
        raise TimeoutError('provider may have accepted first root')

    def configured(patch, child):
        calls = original(patch, child)
        if 'cedar-change-14' in child.feedback[0]:
            patch.setitem(llm_client.BACKENDS, 'fake', timeout)
        return calls

    monkeypatch.setattr('tests.compile_work_completion_helpers._child_backend', configured)


def test_unknown_root_keeps_later_progress_but_blocks_entire_delivery(work, multi, monkeypatch):  # noqa: F811
    unknown_calls = []
    _uncertain_first_backend(monkeypatch, unknown_calls)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _execute(work, multi)
    assert [len(calls) for calls in multi[1]] == [0, 3]
    assert work[0].get_compile_event(multi[0].work_id).stage == 'pending'
    assert work[0].get(work[1].id).blocked_capability == 'provider_outcome_unknown'
    work[0].unblock(work[1].id)
    replacement = work[0].claim('work-recovery')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _execute((work[0], replacement, work[2]), multi, capture=_forbidden)
    assert unknown_calls == [True]
