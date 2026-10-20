"""Queue progress is complete only while source receipts remain authoritative."""
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _dependencies
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_publication_checkpoint import _apply_saved, _resolved, _save


def _confirm(work, ready, saved, coordinator):
    return controller.confirm_compile_publication(work[0], work[1], saved, ready,
        coordinator=coordinator, dependencies=_dependencies())


def test_accepted_plan_without_source_receipts_is_not_complete(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        assert _confirm(work, ready, saved, coordinator) is None
        assert work[0].get_compile_event(context.work_id) == saved


def test_complete_publication_is_journaled_and_revalidated(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        result = _apply_saved(work, ready, saved, coordinator)
        completed = _confirm(work, ready, saved, coordinator)
        assert (completed.stage, completed.outcome) == ('published', 'completed')
        assert completed.details['publication_completion']['transaction_id'] == result.transaction_id
        assert _confirm(work, ready, saved, coordinator) == completed
        assert _save(work, ready, context, resolved) == saved
        assert len(calls) == 3
        assert work[0].get(work[1].id).state == 'leased'


def test_commit_before_queue_journal_failure_recovers_from_receipts(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        result = _apply_saved(work, ready, saved, coordinator)

        def interrupted(*_args):
            raise OSError('completion journal unavailable')

        with monkeypatch.context() as patch:
            patch.setattr(work[0], 'record_compile_event', interrupted)
            with pytest.raises(OSError):
                _confirm(work, ready, saved, coordinator)
        assert work[0].get_compile_event(context.work_id) == saved
        completed = _confirm(work, ready, saved, coordinator)
        assert completed.details['publication_completion']['transaction_id'] == result.transaction_id
        assert len(calls) == 3


def test_missing_receipt_invalidates_a_recorded_completion(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        _apply_saved(work, ready, saved, coordinator)
        completed = _confirm(work, ready, saved, coordinator)
        receipt = next((work[0].vault / 'knowledge/daily/receipts').glob('v4-*.md'))
        receipt.unlink()
        with pytest.raises(ValueError, match='receipt'):
            _confirm(work, ready, saved, coordinator)
        assert work[0].get_compile_event(context.work_id) == completed


def test_receipts_for_another_accepted_action_do_not_complete_this_plan(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        compiler.apply_compile_plan(ready.inputs, resolved.plan, action_key='e' * 64,
            trigger='manual', coordinator=coordinator, batch=ready, provider_budget=resolved.provider_budget)
        with pytest.raises(ValueError, match='publication action'):
            _confirm(work, ready, saved, coordinator)
        assert work[0].get_compile_event(context.work_id) == saved


def test_unrecorded_publication_cannot_be_confirmed(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        _apply_saved(work, ready, saved, coordinator)
        with pytest.raises(ValueError):
            _confirm(work, ready, replace(saved, outcome='completed'), coordinator)


def test_completion_refuses_a_coordinator_for_another_vault(work, compiler_case, monkeypatch, tmp_path):
    import copy

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        foreign = copy.copy(coordinator)
        foreign.vault = tmp_path
        with pytest.raises(ValueError, match='coordinator identity'):
            _confirm(work, ready, saved, foreign)


def test_committed_transaction_without_receipts_is_not_source_completion(work, compiler_case, monkeypatch):
    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        result = _apply_saved(work, ready, saved, coordinator)
        assert coordinator.committed_attempt(result.operation_id).state == 'committed'
        for receipt in (work[0].vault / 'knowledge/daily/receipts').glob('v4-*.md'):
            receipt.unlink()
        assert _confirm(work, ready, saved, coordinator) is None
        assert work[0].get_compile_event(context.work_id) == saved


def test_completed_publication_survives_redrive(work, compiler_case, monkeypatch):
    from tests.test_compile_preflight_consumer import _consume
    from tests.test_compile_task_origin import _redrive

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, calls):
        saved = _save(work, ready, context, resolved)
        _apply_saved(work, ready, saved, coordinator)
        completed = _confirm(work, ready, saved, coordinator)
        source = context.details['compile_context']
        snapshot = work[0].get_compile_event(source['snapshot_work_id'], sequence=source['snapshot_sequence'])
    replacement = _redrive(work[0], work[1])
    resumed = (work[0], replacement, work[2])
    items = _consume(resumed, snapshot)
    with controller.open_prepared_compile_batch(work[0], replacement, next(items), snapshot,
            dependencies=_dependencies()) as (ready, context):
        assert _confirm(resumed, ready, saved, coordinator) == completed
        assert _save(resumed, ready, context, resolved) == saved
        assert len(calls) == 3
    items.close()


def test_another_plan_for_same_model_request_cannot_confirm_saved_intent(work, compiler_case, monkeypatch):
    import copy
    import json

    with _resolved(work, compiler_case, monkeypatch) as (ready, context, resolved, coordinator, _calls):
        saved = _save(work, ready, context, resolved)
        other_plan = copy.deepcopy(resolved.plan)
        semantic = json.loads(other_plan['operations'][0]['content'])
        semantic['body_markdown'] += '\nA separately accepted wording.'
        other_plan['operations'][0]['content'] = compiler.claim_json_bytes(semantic).decode('utf-8')
        other = replace(resolved, plan=other_plan)
        other_key = controller._publication_content(other)['action_key']
        compiler.apply_compile_plan(ready.inputs, other_plan, action_key=other_key, trigger='manual',
            coordinator=coordinator, batch=ready, provider_budget=other.provider_budget)
        with pytest.raises(ValueError, match='publication action'):
            _confirm(work, ready, saved, coordinator)
        assert work[0].get_compile_event(context.work_id) == saved
