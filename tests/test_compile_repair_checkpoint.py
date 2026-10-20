"""Repair children retain exact source, context and feedback authority across restart."""
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _consume, _dependencies, _snapshot
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_repair_capacity_repartitions_complete_source_work import _batch, _records


@pytest.fixture
def repair(work, compiler_case):
    original, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, original.inputs)
    items = _consume(work, snapshot)
    try:
        with controller.open_prepared_compile_batch(work[0], work[1], next(items), snapshot,
                dependencies=_dependencies()) as (batch, context):
            yield batch, context, compiler._CompileRepairCapacityError('input_capacity', feedback)
    finally:
        items.close()


def _save(work, repair):
    batch, context, failure = repair
    return controller.save_compile_repair(work[0], work[1], context, batch, failure,
        dependencies=_dependencies())


def _read(work, repair, saved):
    return controller.read_compile_repair(work[0], work[1], saved, repair[1], repair[0],
        dependencies=_dependencies())


def test_repair_decision_preserves_complete_sources_context_and_feedback(work, repair):
    saved = _save(work, repair)
    children = _read(work, repair, saved)
    assert (saved.stage, saved.outcome) == ('review_ready', 'waiting')
    assert len(children) == 2
    _assert_sources_and_feedback(children, repair)
    identities = _child_work_ids(children)
    assert len(set(identities)) == len(children)
    assert _child_work_ids(_read(work, repair, saved)) == identities
    assert _save(work, repair) == saved
    assert work[0].get(work[1].id).state == 'leased'
    _assert_child_context(children, repair[0])


def _child_work_ids(children):
    return [child.work_id for child in children]


def _assert_sources_and_feedback(children, repair):
    assert [part for child in children for part in child.batch.inputs.dailies] == list(repair[0].inputs.dailies)
    combined = tuple(record for child in children for record in child.feedback)
    assert _records(combined, 'operations') == _records(repair[2].feedback, 'operations')
    assert _records(combined, 'reviews') == _records(repair[2].feedback, 'reviews')


def _assert_child_context(children, parent):
    for child in children:
        assert child.batch.inputs.targets == parent.inputs.targets
        assert child.batch.inputs.vault_files == parent.inputs.vault_files


def test_repair_orphan_recovers_without_replacing_decision(work, repair, monkeypatch):
    def interrupted(*args):
        raise OSError('journal unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError):
            _save(work, repair)
    saved = _save(work, repair)
    assert len(_read(work, repair, saved)) == 2
    assert _save(work, repair) == saved


def test_changed_feedback_cannot_replace_retained_repair(work, repair):
    saved = _save(work, repair)
    failure = compiler._CompileRepairCapacityError('input_capacity', ())
    with pytest.raises(ValueError, match='repair.*identity'):
        _save(work, (*repair[:2], failure))
    assert work[0].get_compile_event(saved.work_id) == saved


def test_uncertain_dispatch_cannot_authorize_repartition(work, repair, monkeypatch):
    context = repair[1]
    dispatch = controller._dispatch_event(context, work[1].id, 'e' * 64)
    work[0].record_compile_event(work[1], dispatch)

    def forbidden(*args):
        raise AssertionError('uncertain call repartitioned')

    monkeypatch.setattr(compiler, '_repartition_compile_repair', forbidden)
    with pytest.raises(ValueError, match='ready semantic checkpoint'):
        _save(work, repair)
    assert work[0].get_compile_event(context.work_id) == dispatch


def test_repair_reader_requires_exact_recorded_decision(work, repair):
    saved = _save(work, repair)
    with pytest.raises(ValueError):
        _read(work, repair, replace(saved, outcome='ready'))


def test_invalid_partition_cannot_omit_or_duplicate_source_records(work, repair, monkeypatch):
    children = compiler._repartition_compile_repair(repair[0], repair[2].feedback)
    monkeypatch.setattr(compiler, '_repartition_compile_repair', lambda *args: (children[0], children[0]))
    with pytest.raises(ValueError, match='source coverage'):
        _save(work, repair)
    assert work[0].get_compile_event(repair[1].work_id) == repair[1]


def test_indivisible_review_is_retained_without_fake_child_success(work, repair):
    import json

    record = json.loads(repair[2].feedback[0])
    spanning = record['operations'][0]
    spanning['evidence'] = [item['evidence'][0] for item in record['operations']]
    feedback = compiler._validated_critique_feedback([spanning], record['reviews'][:1])
    failure = compiler._CompileRepairCapacityError('indivisible correction', feedback)
    original = (*repair[:2], failure)
    saved = _save(work, original)
    assert _read(work, original, saved) == ()
    assert saved.outcome == 'waiting'
    assert work[0].get(work[1].id).state == 'leased'


def test_reader_rejects_partition_drift_after_decision(work, repair, monkeypatch):
    saved = _save(work, repair)
    monkeypatch.setattr(compiler, '_repartition_compile_repair', lambda *args: ())
    with pytest.raises(ValueError, match='partition changed'):
        _read(work, repair, saved)


def test_saved_repair_is_not_a_ready_model_call_checkpoint(work, repair):
    saved = _save(work, repair)
    with pytest.raises(ValueError, match='ready semantic checkpoint'):
        controller._require_checkpoint(saved)
