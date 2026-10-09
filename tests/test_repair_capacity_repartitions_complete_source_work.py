"""A full repair can partition work only without losing source bytes or reviewed proposals."""
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import compile_memory as compiler
import pytest

from tests.test_compile_critic_drop_cannot_complete_source_work import _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def _batch(root):
    paths = []
    operations = []
    for day in (14, 15, 16):
        date = f'2026-07-{day}'
        quote = f'Project Cedar recorded supported change {day}.'
        path = root / f'knowledge/daily/{date}.md'
        path.write_text(f'## [10:00:00] session-end | manual\n{quote}\n')
        paths.append(path)
        operation = _proposed(f'cedar-change-{day}')
        operation['body_markdown'] = quote
        operation['evidence'][0].update(daily_date=date, quoted_text=quote, claim=quote)
        operations.append(operation)
    note = root / 'knowledge/notes/context.md'
    note.write_text('---\ntype: pattern\n---\n# Context\nKeep the complete project context.\n')
    inputs = compiler.snapshot_compile_inputs(paths)
    batch = compiler.pack_compile_batches(inputs, model=None)[0]
    reviews = [dict(slug=item['slug'], verdict='drop', reason='Correct its supported scope.')
               for item in operations]
    return batch, compiler._validated_critique_feedback(operations, reviews)


def _records(feedback, name):
    return sorted(json.dumps(item, sort_keys=True) for record in feedback
                  for item in json.loads(record)[name])


def _parts(children):
    return [part for child, _feedback in children for part in child.inputs.dailies]


def _combined_feedback(children):
    return tuple(record for _child, feedback in children for record in feedback)


def test_repartition_keeps_each_physical_part_and_complete_reviewed_proposal_once(vault):
    root, _state = vault
    original, feedback = _batch(root)

    children = compiler._repartition_compile_repair(original, feedback)

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
    protected = compiler._validated_critique_feedback([spanning], record['reviews'][:1])

    assert compiler._repartition_compile_repair(original, protected) == ()
    assert len(spanning['evidence']) == 3


def test_an_indivisible_source_keeps_its_capacity_failure(vault):
    root, _state = vault
    original, feedback = _batch(root)
    part = original.inputs.dailies[0]
    child = compiler._compile_batch(original.inputs, {part.part_key},
        compiler._packing_budget(original.packing, None), None, None)

    assert compiler._repartition_compile_repair(child, feedback) == ()


def test_runner_executes_all_complete_children_and_preserves_the_failure_reason(vault, monkeypatch, capsys):
    root, _state = vault
    original, feedback = _batch(root)
    refusal = compiler._CompileRepairCapacityError('draft:codex:input_budget', feedback)
    run = Mock(side_effect=[refusal, compiler.BatchOutcome(0, 'published', 1),
                          compiler.BatchOutcome(0, 'published', 2)])
    monkeypatch.setattr(compiler, '_run_batch', run)
    monkeypatch.setattr(compiler, '_refresh_compile_batch', lambda batch, **_kwargs: batch)

    outcomes = compiler._run_repairable_batch(original, SimpleNamespace(),
        coordinator=None, deadline=math.inf, cancelled=None, owner=None)

    assert outcomes == [compiler.BatchOutcome(0, 'published', 1), compiler.BatchOutcome(0, 'published', 2)]
    assert run.call_count == 3
    assert all(call.kwargs['critic_feedback'] for call in run.call_args_list[1:])
    assert 'draft:codex:input_budget' in capsys.readouterr().out


def test_non_capacity_failures_do_not_gain_a_repartition_retry(vault, monkeypatch):
    root, _state = vault
    original, _feedback = _batch(root)
    run = Mock(return_value=compiler.BatchOutcome(1))
    monkeypatch.setattr(compiler, '_run_batch', run)

    assert compiler._run_repairable_batch(original, SimpleNamespace(),
        coordinator=None, deadline=math.inf, cancelled=None, owner=None) == [compiler.BatchOutcome(1)]
    run.assert_called_once()


def test_real_resolution_retains_validated_feedback_with_its_capacity_failure(vault):
    root, _state = vault
    _original, feedback = _batch(root)
    attempt = SimpleNamespace(lineage=('critique:codex:validation_error', 'draft:codex:input_budget'),
                              critic_feedback=feedback)

    error = compiler._unresolved_compile_error(attempt)

    assert isinstance(error, compiler._CompileRepairCapacityError)
    assert error.feedback == feedback
    assert 'critique:codex:validation_error' in str(error)
    assert 'draft:codex:input_budget' in str(error)


@pytest.mark.parametrize('failure,feedback', [
    ('draft:codex:input_budget', ()),
    ('critique:codex:input_budget', ('validated review',)),
    ('draft:codex:validation_error', ('validated review',)),
    ('probe:codex:unavailable', ('validated review',)),
])
def test_unrelated_failures_keep_the_original_failure_boundary(failure, feedback):
    error = compiler._unresolved_compile_error(SimpleNamespace(lineage=(failure,), critic_feedback=feedback))

    assert type(error) is RuntimeError
    assert failure in str(error)


def test_lost_context_fails_its_child_without_hiding_or_discarding_its_sibling(vault, monkeypatch):
    root, _state = vault
    original, feedback = _batch(root)
    children = compiler._repartition_compile_repair(original, feedback)
    validate = Mock(side_effect=[ValueError('lost selected context'), None])
    record = Mock(return_value=1)
    run = Mock(return_value=[compiler.BatchOutcome(0, 'published', 1)])
    monkeypatch.setattr(compiler, '_refresh_compile_batch', lambda batch, **_kwargs: batch)
    monkeypatch.setattr(compiler, '_require_repair_context', validate)
    monkeypatch.setattr(compiler, '_record_failed_batch', record)
    monkeypatch.setattr(compiler, '_run_repairable_batch', run)

    outcomes = compiler._run_repair_children(children, SimpleNamespace(),
        dict(coordinator=None, deadline=math.inf, cancelled=None, owner=None))

    assert outcomes == [compiler.BatchOutcome(1), compiler.BatchOutcome(0, 'published', 1)]
    assert record.call_args.args[0] == children[0][0].inputs
    assert run.call_args.args[0] == children[1][0]
    run.assert_called_once()
