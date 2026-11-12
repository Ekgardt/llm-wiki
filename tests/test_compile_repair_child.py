"""Repair children inherit authority and feedback, never a fresh mutable snapshot."""
from dataclasses import replace

import compile_controller as controller
import compile_progress as progress
import pytest
from llm_wiki import compile_pipeline_batches
from markdown_transaction import active_markdown_coordinator
from queue_core import (
    LeaseFenceError,
)

from tests.compile_call_controller_helpers import work as work
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_repair_checkpoint_helpers import _read, _save
from tests.compile_repair_checkpoint_helpers import repair as repair
from tests.compile_repair_child_helpers import _child_backend


def _child(work, repair):
    return _read(work, repair, _save(work, repair))[0]


def _begin(work, repair, child):
    return controller.begin_compile_repair_batch(work[0], work[1], child, repair[1], repair[0],
        dependencies=_dependencies())


def test_child_admission_reuses_exact_parent_context_artifact(work, repair):
    child = _child(work, repair)
    saved = _begin(work, repair, child)
    assert saved.work_id == child.work_id
    assert saved.details['artifact'] == repair[1].details['artifact']
    assert saved.details['compile_context'] == repair[1].details['compile_context']
    assert saved.details['repair_context']['parent_digest'] == progress.event_digest(repair[1])
    assert _begin(work, repair, child) == saved
    assert work[0].get(work[1].id).state == 'leased'


@pytest.mark.parametrize('field', ['work_id', 'entry_digest', 'feedback', 'context'])
def test_child_mutation_cannot_gain_admission(work, repair, field):
    child = _child(work, repair)
    changed = _changed_child(child, field)
    with pytest.raises(ValueError):
        _begin(work, repair, changed)
    assert work[0].get_compile_event(child.work_id) is None


def _changed_child(child, field):
    if field == 'context':
        inputs = replace(child.batch.inputs, targets=())
        return replace(child, batch=replace(child.batch, inputs=inputs))
    return replace(child, **{field: {'work_id': 'e' * 64, 'entry_digest': 'f' * 64, 'feedback': ()}[field]})


def test_child_admission_recovers_between_admission_and_context(work, repair, monkeypatch):
    child = _child(work, repair)
    record = work[0].record_compile_event

    def interrupted(lease, event):
        if event.sequence == 1:
            raise OSError('context event unavailable')
        return record(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError):
            _begin(work, repair, child)
    admitted = work[0].get_compile_event(child.work_id)
    assert admitted.sequence == 0
    saved = _begin(work, repair, child)
    assert saved.sequence == 1
    assert work[0].get_compile_event(child.work_id, sequence=0) == admitted


def _forbidden(*args, **kwargs):
    raise AssertionError('repair recaptured context or repeated accepted semantic work')


def test_child_executes_with_retained_feedback_and_recovers_without_resolution(work, repair, monkeypatch):
    child = _child(work, repair)
    calls = _child_backend(monkeypatch, child)
    coordinator = active_markdown_coordinator(work[0].vault, work[0].state_root)
    resolve = compile_pipeline_batches.resolve_compile_plan
    received = []

    def checked(*args, **kwargs):
        received.append(kwargs['critic_feedback'])
        return resolve(*args, **kwargs)

    point_compile_path(monkeypatch, 'resolve_compile_plan', checked)
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', _forbidden)
    options = dict(coordinator=coordinator, dependencies=_dependencies(), trigger='manual')
    completed = controller.execute_repair_compile_batch(work[0], work[1], child, repair[1], repair[0], **options)
    assert completed.stage == 'published'
    assert received == [child.feedback]
    assert len(calls) == 3
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert controller.execute_repair_compile_batch(work[0], work[1], child, repair[1], repair[0], **options) == completed
    assert work[0].get(work[1].id).state == 'leased'


def test_stale_lease_cannot_admit_child(work, repair):
    child = _child(work, repair)
    with pytest.raises(LeaseFenceError, match='lease is stale or not owned'):
        _begin((work[0], replace(work[1], token='stale'), work[2]), repair, child)
    assert work[0].get_compile_event(child.work_id) is None
