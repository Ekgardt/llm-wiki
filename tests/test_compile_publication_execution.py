"""Execute only the saved intent, and recover commits before any further write."""
import compile_controller as controller
import pytest
from llm_wiki import compile_pipeline_apply

from tests.compile_call_controller_helpers import work as work
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_publication_checkpoint_helpers import _apply_saved, _resolved, _save


def _execute(work, ready, saved, coordinator, **kwargs):
    return controller.execute_compile_publication(work[0], work[1], saved, ready,
        coordinator=coordinator, dependencies=_dependencies(), trigger='manual', **kwargs)


def test_execute_saved_plan_and_recover_without_applying_again(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        completed = _execute(work, ready, saved, coordinator)
        assert completed.stage == 'published'

        def forbidden(*args, **kwargs):
            raise AssertionError('already committed publication applied again')

        point_compile_path(monkeypatch, 'apply_compile_plan', forbidden)
        assert _execute(work, ready, saved, coordinator) == completed
        assert len(calls) == 3
        assert work[0].get(work[1].id).state == 'leased'


def test_commit_before_execution_recovers_without_apply(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        committed = _apply_saved(work, ready, saved, coordinator)

        def forbidden(*args, **kwargs):
            raise AssertionError('commit recovery attempted another publication')

        point_compile_path(monkeypatch, 'apply_compile_plan', forbidden)
        completed = _execute(work, ready, saved, coordinator)
        assert completed.details['publication_completion']['transaction_id'] == committed.transaction_id
        assert len(calls) == 3


def test_execution_does_not_trust_apply_return_without_receipts(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        point_compile_path(monkeypatch, 'apply_compile_plan', lambda *args, **kwargs: object())
        assert _execute(work, ready, saved, coordinator) is None
        assert work[0].get_compile_event(context.work_id) == saved


def test_execution_preserves_failed_publication_for_resume(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)

        def unavailable(*args, **kwargs):
            raise OSError('publication storage unavailable')

        with monkeypatch.context() as patch, pytest.raises(OSError, match='storage unavailable'):
            point_compile_path(patch, 'apply_compile_plan', unavailable)
            _execute(work, ready, saved, coordinator)
        assert work[0].get_compile_event(context.work_id) == saved
        assert _execute(work, ready, saved, coordinator).stage == 'published'
        assert len(calls) == 3


@pytest.mark.parametrize('failure', ['cancelled', 'owner_lost', 'dependencies_changed'])
def test_execution_rechecks_admission_inside_apply(work, compiler_case, monkeypatch, failure):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        apply = compile_pipeline_apply.apply_compile_plan
        cancelled = []

        def refused(*args, **kwargs):
            raise ValueError('publication admission lost')

        def interrupted(*args, **kwargs):
            _invalidate_admission(work, patch, cancelled, failure, refused)
            return apply(*args, **kwargs)

        with monkeypatch.context() as patch, pytest.raises((ValueError, TimeoutError)):
            point_compile_path(patch, 'apply_compile_plan', interrupted)
            _execute(work, ready, saved, coordinator, cancelled=lambda: bool(cancelled))
        assert not (work[0].vault / 'knowledge/notes/cedar-merge-order.md').exists()
        assert work[0].get_compile_event(context.work_id) == saved


def _invalidate_admission(work, monkeypatch, cancelled, failure, refused):
    if failure == 'cancelled':
        cancelled.append(True)
        return
    targets = {'owner_lost': (work[0], 'verify_compile_checkpoint_owner'),
               'dependencies_changed': (controller.CompileDependencies, 'verify')}
    target, attribute = targets[failure]
    monkeypatch.setattr(target, attribute, refused)
