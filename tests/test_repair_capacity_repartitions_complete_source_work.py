"""A full repair can partition work only without losing source bytes or reviewed proposals."""
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_batches, compile_pipeline_claims, compile_pipeline_prompts

from tests.compile_paths import point_compile_path
from tests.repair_capacity_repartitions_complete_source_work_helpers import (
    _batch,
    _combined_feedback,
    _parts,
    _records,
)
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import vault as vault


def test_structural_diagnostic_survives_every_repair_partition(vault):
    root, _state = vault
    original, reviewed = _batch(root)
    diagnostic = json.dumps({'stage': 'draft', 'validation_error': 'related[0]: expected string',
                             'rejected_response': '{"operations": ['})
    children = compile_pipeline.repartition_compile_repair(original, (*reviewed, diagnostic))
    assert len(children) == 2
    assert _parts(children) == list(original.inputs.dailies)
    for _child, feedback in children:
        assert diagnostic in feedback


def test_repartition_keeps_each_physical_part_and_complete_reviewed_proposal_once(vault):
    root, _state = vault
    original, feedback = _batch(root)

    children = compile_pipeline.repartition_compile_repair(original, feedback)

    assert len(children) == 2
    parts = _parts(children)
    assert parts == list(original.inputs.dailies)
    assert len({part.part_key for part in parts}) == len(parts)
    for child, child_feedback in children:
        assert child.inputs.targets == original.inputs.targets
        assert child.inputs.vault_files == original.inputs.vault_files
        assert original.inputs.sources[-1] in child.inputs.sources
        assert 0 < len(child.inputs.dailies) < len(original.inputs.dailies)
        assert child_feedback
    combined = _combined_feedback(children)
    assert _records(combined, 'operations') == _records(feedback, 'operations')
    assert _records(combined, 'reviews') == _records(feedback, 'reviews')


def test_a_proposal_spanning_every_unit_cannot_be_silently_cut(vault):
    root, _state = vault
    original, feedback = _batch(root)
    record = json.loads(feedback[0])
    spanning = record['operations'][0]
    spanning['evidence'] = [item['evidence'][0] for item in record['operations']]
    protected = compile_pipeline_prompts.validated_critique_feedback([spanning], record['reviews'][:1])

    assert compile_pipeline.repartition_compile_repair(original, protected) == ()
    assert len(spanning['evidence']) == 3


def test_an_indivisible_source_keeps_its_capacity_failure(vault):
    root, _state = vault
    original, feedback = _batch(root)
    part = original.inputs.dailies[0]
    child = compile_pipeline_batches.compile_batch(original.inputs, {part.part_key},
        compile_pipeline_prompts.packing_budget(original.packing, None), None, None)

    assert compile_pipeline.repartition_compile_repair(child, feedback) == ()


def test_runner_executes_all_complete_children_and_preserves_the_failure_reason(vault, monkeypatch, capsys):
    root, _state = vault
    original, feedback = _batch(root)
    refusal = compile_pipeline_batches.CompileRepairCapacityError('draft:codex:input_budget', feedback)
    run = Mock(side_effect=[refusal, compile_pipeline_claims.BatchOutcome(0, 'published', 1),
                          compile_pipeline_claims.BatchOutcome(0, 'published', 2)])
    point_compile_path(monkeypatch, 'run_batch', run)
    point_compile_path(monkeypatch, 'refresh_compile_batch', lambda batch, **_kwargs: batch)

    outcomes = compile_pipeline.run_repairable_batch(original, SimpleNamespace(),
        coordinator=None, deadline=math.inf, cancelled=None, owner=None)

    assert outcomes == [compile_pipeline_claims.BatchOutcome(0, 'published', 1), compile_pipeline_claims.BatchOutcome(0, 'published', 2)]
    assert run.call_count == 3
    assert all(call.kwargs['critic_feedback'] for call in run.call_args_list[1:])
    assert 'draft:codex:input_budget' in capsys.readouterr().out


def test_non_capacity_failures_do_not_gain_a_repartition_retry(vault, monkeypatch):
    root, _state = vault
    original, _feedback = _batch(root)
    run = Mock(return_value=compile_pipeline_claims.BatchOutcome(1))
    point_compile_path(monkeypatch, 'run_batch', run)

    assert compile_pipeline.run_repairable_batch(original, SimpleNamespace(),
        coordinator=None, deadline=math.inf, cancelled=None, owner=None) == [compile_pipeline_claims.BatchOutcome(1)]
    run.assert_called_once()


def test_real_resolution_retains_validated_feedback_with_its_capacity_failure(vault):
    root, _state = vault
    _original, feedback = _batch(root)
    attempt = SimpleNamespace(lineage=('critique:codex:validation_error', 'draft:codex:input_budget'),
                              critic_feedback=feedback)

    error = compile_pipeline_batches.unresolved_compile_error(attempt)

    assert isinstance(error, compile_pipeline_batches.CompileRepairCapacityError)
    assert error.feedback == feedback
    assert 'critique:codex:validation_error' in str(error)
    assert 'draft:codex:input_budget' in str(error)


@pytest.mark.parametrize('failure,feedback', [
    ('draft:codex:validation_error', ('validated review',)),
    ('probe:codex:unavailable', ('validated review',)),
])
def test_unrelated_failures_keep_the_original_failure_boundary(failure, feedback):
    error = compile_pipeline_batches.unresolved_compile_error(SimpleNamespace(lineage=(failure,), critic_feedback=feedback))

    assert type(error) is compile_pipeline_batches.CompilePlanUnavailable
    assert isinstance(error, RuntimeError)
    assert not isinstance(error, compile_pipeline_batches.CompileRepairCapacityError)
    assert failure in str(error)


@pytest.mark.parametrize('stage', ['draft', 'critique'])
def test_source_capacity_failure_can_split_before_any_semantic_review(stage):
    lineage = (f'{stage}:codex:input_budget', 'probe:claude:unavailable')
    error = compile_pipeline_batches.unresolved_compile_error(SimpleNamespace(lineage=lineage, critic_feedback=()))
    assert isinstance(error, compile_pipeline_batches.CompileRepairCapacityError)
    assert error.feedback == ()
    assert 'probe:claude:unavailable' in str(error)


@pytest.mark.parametrize('dry_run', [False, True])
def test_lost_context_fails_its_child_without_hiding_or_discarding_its_sibling(vault, monkeypatch, dry_run):
    root, _state = vault
    original, feedback = _batch(root)
    children = compile_pipeline.repartition_compile_repair(original, feedback)
    validate = Mock(side_effect=[ValueError('lost selected context'), None])
    record = Mock(return_value=1)
    run = Mock(return_value=[compile_pipeline_claims.BatchOutcome(0, 'published', 1)])
    point_compile_path(monkeypatch, 'refresh_compile_batch', lambda batch, **_kwargs: batch)
    point_compile_path(monkeypatch, 'require_repair_context', validate)
    point_compile_path(monkeypatch, 'record_failed_batch', record)
    point_compile_path(monkeypatch, 'run_repairable_batch', run)

    outcomes = compile_pipeline.run_repair_children(children, SimpleNamespace(dry_run=dry_run),
        dict(coordinator=None, deadline=math.inf, cancelled=None, owner=None))

    assert outcomes == [compile_pipeline_claims.BatchOutcome(1), compile_pipeline_claims.BatchOutcome(0, 'published', 1)]
    assert record.call_args.args[0] == children[0][0].inputs
    assert record.call_args.kwargs["record"] is (not dry_run)
    assert run.call_args.args[0] == children[1][0]
    run.assert_called_once()
