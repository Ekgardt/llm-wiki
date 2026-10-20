"""Composite completion revalidates every leaf receipt under one writer gate."""
import compile_controller as controller
import compile_memory as compiler
import pytest
from markdown_transaction import active_markdown_coordinator

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _dependencies
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_repair_checkpoint import _read, _save
from tests.test_compile_repair_checkpoint import repair as repair
from tests.test_compile_repair_child import _child_backend


def _coordinator(work):
    return active_markdown_coordinator(work[0].vault, work[0].state_root)


def _confirm(work, repair, saved):
    return controller.confirm_compile_repair(work[0], work[1], saved, repair[1], repair[0],
        coordinator=_coordinator(work), dependencies=_dependencies())


def _publish(work, parent_batch, parent_context, child, monkeypatch):
    calls = _child_backend(monkeypatch, child)
    result = controller.execute_repair_compile_batch(work[0], work[1], child, parent_context, parent_batch,
        coordinator=_coordinator(work), dependencies=_dependencies(), trigger='manual')
    assert len(calls) == 3
    return result


def test_missing_children_do_not_complete_or_gain_admission(work, repair):
    saved = _save(work, repair)
    children = _read(work, repair, saved)
    assert _confirm(work, repair, saved) is None
    for child in children:
        assert work[0].get_compile_event(child.work_id) is None
    assert work[0].get_compile_event(saved.work_id) == saved


def test_one_completed_child_cannot_complete_parent(work, repair, monkeypatch):
    saved = _save(work, repair)
    first = _read(work, repair, saved)[0]
    _publish(work, repair[0], repair[1], first, monkeypatch)
    assert _confirm(work, repair, saved) is None


def _publish_children(work, repair, saved, monkeypatch):
    return [_publish(work, repair[0], repair[1], child, monkeypatch)
            for child in _read(work, repair, saved)]


def test_parent_completion_binds_children_and_rechecks_missing_receipt(work, repair, monkeypatch):
    saved = _save(work, repair)
    children = _publish_children(work, repair, saved, monkeypatch)
    completed = _confirm(work, repair, saved)
    assert (completed.stage, completed.outcome) == ('published', 'completed')
    assert [record['event_digest'] for record in completed.details['repair_completion']['children']] == [
        controller.progress._event_digest(child) for child in children]
    assert _confirm(work, repair, saved) == completed
    receipt = next((work[0].vault / 'knowledge/daily/receipts').glob('v4-*.md'))
    receipt.unlink()
    with pytest.raises(ValueError, match='receipt'):
        _confirm(work, repair, saved)
    assert work[0].get_compile_event(saved.work_id) == completed
    assert work[0].get(work[1].id).state == 'leased'


def test_completion_journal_failure_recovers_from_child_receipts(work, repair, monkeypatch):
    saved = _save(work, repair)
    _publish_children(work, repair, saved, monkeypatch)
    record = work[0].record_compile_event

    def interrupted(lease, event):
        if event.work_id == saved.work_id:
            raise OSError('parent journal unavailable')
        return record(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError):
            _confirm(work, repair, saved)
    assert work[0].get_compile_event(saved.work_id) == saved
    assert _confirm(work, repair, saved).stage == 'published'


def test_nested_repair_completion_revalidates_grandchild_receipts(work, repair, monkeypatch):
    saved = _save(work, repair)
    first, branch = _read(work, repair, saved)
    _publish(work, repair[0], repair[1], first, monkeypatch)
    context = controller.begin_compile_repair_batch(work[0], work[1], branch, repair[1], repair[0],
        dependencies=_dependencies())
    failure = compiler._CompileRepairCapacityError('nested capacity', branch.feedback)
    nested = controller.save_compile_repair(work[0], work[1], context, branch.batch, failure,
        dependencies=_dependencies())
    children = controller.read_compile_repair(work[0], work[1], nested, context, branch.batch,
        dependencies=_dependencies())
    assert len(children) == 2
    for child in children:
        _publish(work, branch.batch, context, child, monkeypatch)
    completed = _confirm(work, repair, saved)
    assert completed.stage == 'published'
    assert work[0].get_compile_event(branch.work_id).stage == 'published'
    assert _confirm(work, repair, saved) == completed


def test_all_leaf_proofs_use_one_writer_gate(work, repair, monkeypatch):
    from contextlib import contextmanager

    saved = _save(work, repair)
    _publish_children(work, repair, saved, monkeypatch)
    original = compiler._compile_writer_gate
    entered = []

    @contextmanager
    def counted(*args, **kwargs):
        entered.append(True)
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(compiler, '_compile_writer_gate', counted)
    assert _confirm(work, repair, saved).stage == 'published'
    assert entered == [True]


def test_expired_confirmation_never_records_parent_completion(work, repair):
    saved = _save(work, repair)
    with pytest.raises(TimeoutError):
        controller.confirm_compile_repair(work[0], work[1], saved, repair[1], repair[0],
            coordinator=_coordinator(work), dependencies=_dependencies(), deadline=0)
    assert work[0].get_compile_event(saved.work_id) == saved


def test_saved_repair_remains_idempotent_after_parent_completion(work, repair, monkeypatch):
    saved = _save(work, repair)
    _publish_children(work, repair, saved, monkeypatch)
    completed = _confirm(work, repair, saved)
    assert _save(work, repair) == saved
    assert work[0].get_compile_event(saved.work_id) == completed
