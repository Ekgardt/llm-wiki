"""A higher-ranked new page cannot replace context already used by the critic."""
import math
from types import SimpleNamespace
from unittest.mock import Mock

import compile_memory as compiler
from context_budget import ContextBudget

from tests.test_repair_capacity_repartitions_complete_source_work import _batch
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def _reviewed_batch(root):
    original, feedback = _batch(root)
    note = root / 'knowledge/notes/context.md'
    note.write_text('---\ntype: pattern\n---\n# Context\n' + 'Preserve the established working agreement.\n' * 150)
    part = original.inputs.dailies[0]
    inputs = compiler.snapshot_compile_inputs([root / part.logical_path])
    keys = {part.part_key for part in inputs.dailies}
    selected = compiler._subset_compile_inputs(inputs, keys, {'knowledge/notes/context.md'})
    measured = compiler._draft_prompt_count(selected, None, None)
    budget = ContextBudget(None, measured.tokens, 0, 0)
    batch = compiler._compile_batch(inputs, keys, budget, None, None,
                                    optional_paths={'knowledge/notes/context.md'})
    preferred = root / 'knowledge/notes/new-preferred.md'
    preferred.write_text('---\ntype: pattern\n---\n# Cedar supported change\n' +
                         'Project Cedar recorded supported change 14.\n' * 130)
    return batch, feedback


def test_repair_refresh_keeps_reviewed_context_when_new_ranking_would_replace_it(vault, monkeypatch):
    root, _state = vault
    batch, feedback = _reviewed_batch(root)
    ordinary = compiler._refresh_compile_batch(batch)
    assert 'knowledge/notes/context.md' not in compiler._repair_context_paths(ordinary.inputs)
    assert 'knowledge/notes/new-preferred.md' in compiler._repair_context_paths(ordinary.inputs)
    run = Mock(return_value=[compiler.BatchOutcome(0, 'published', 1)])
    record = Mock(return_value=1)
    monkeypatch.setattr(compiler, '_run_repairable_batch', run)
    monkeypatch.setattr(compiler, '_record_failed_batch', record)

    outcomes = compiler._run_repair_child(batch, feedback, SimpleNamespace(),
        dict(coordinator=None, deadline=math.inf, cancelled=None, owner=None))

    assert outcomes == [compiler.BatchOutcome(0, 'published', 1)]
    refreshed = run.call_args.args[0]
    assert compiler._repair_context_paths(batch.inputs).issubset(compiler._repair_context_paths(refreshed.inputs))
    assert refreshed.manifest == batch.manifest
    assert run.call_args.kwargs['critic_feedback'] == feedback
    record.assert_not_called()


def test_retained_context_refresh_reads_current_bytes_and_current_targets(vault):
    root, _state = vault
    batch, _feedback = _reviewed_batch(root)
    note = root / 'knowledge/notes/context.md'
    current = b'---\ntype: pattern\n---\n# Revised context\nKeep the new verified agreement.\n'
    note.write_bytes(current)

    refreshed = compiler._refresh_compile_batch(batch, retained_context=True)

    source = next(item for item in refreshed.inputs.sources if item.logical_path == 'knowledge/notes/context.md')
    assert source.content == current
    assert source.sha256 == compiler.sha256_bytes(current)
    assert refreshed.inputs.targets == compiler.snapshot_compile_inputs(()).targets
    assert refreshed.manifest == batch.manifest


def test_a_missing_reviewed_page_remains_an_explicit_failure(vault):
    import pytest

    root, _state = vault
    batch, _feedback = _reviewed_batch(root)
    (root / 'knowledge/notes/context.md').unlink()

    with pytest.raises(ValueError, match='complete selected context was lost'):
        compiler._refresh_compile_batch(batch, retained_context=True)
