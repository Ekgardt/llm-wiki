"""Fresh context can change measured size without changing declared capacity."""
from dataclasses import replace

import compile_memory as compiler

from tests.test_codex_planning_basis_owns_the_selected_model import _basis
from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required
from tests.test_compile_keeps_physically_overlapping_existing_context import vault as vault
from tests.test_compile_packing_counts_original_entry_metadata import _inputs


def test_declared_budget_does_not_shrink_when_mandatory_input_is_smaller(tmp_path):
    basis = _basis(tmp_path)
    target = compiler._compile_budget(basis.model)
    actual = compiler._complete_source_budget(target, 1000, set(), (basis.descriptor,))
    assert actual == target


def test_refresh_preserves_capacity_with_new_complete_context(tmp_path, monkeypatch):
    basis = _basis(tmp_path)
    batch = compiler._pack_compile_batches(_inputs(), model=basis.model,
        planning_candidates=(basis.descriptor,), context_pending=True)[0]
    raw = b'A fresh context page.\n'
    note = compiler.SourceSnapshot('knowledge/notes/fresh-context.md', raw, compiler.sha256_bytes(raw))
    target = compiler.TargetSnapshot(note.logical_path, raw, note.sha256)
    context = compiler.CompileInputs((), (note,), (target,))
    monkeypatch.setattr(compiler, 'snapshot_compile_inputs', lambda paths: context)
    refreshed = compiler._refresh_compile_batch(batch)
    assert refreshed.packing.max_input_tokens == batch.packing.max_input_tokens
    assert refreshed.packing.measured_input_tokens != batch.packing.measured_input_tokens
    assert note in refreshed.inputs.sources
    assert refreshed.inputs.targets == (target,)


def test_capacity_still_expands_only_for_complete_required_input(tmp_path):
    basis = _basis(tmp_path)
    target = compiler._compile_budget(basis.model)
    measured = target.max_input_tokens + 1000
    expected = measured + target.reserved_output_tokens + target.safety_margin_tokens
    actual = compiler._complete_source_budget(target, measured, set(), (basis.descriptor,))
    assert actual == replace(target, max_input_tokens=expected)
    limited = replace(basis, planning_window=expected - 1)
    assert compiler._complete_source_budget(target, measured, set(), (limited.descriptor,)) == target


def test_new_required_context_replans_measured_capacity_under_same_provider(vault, tmp_path):
    root, _state = vault
    inputs = _large_required(root)
    basis = _basis(tmp_path)
    batch = compiler._pack_compile_batches(inputs, model=basis.model,
        planning_candidates=(basis.descriptor,), context_pending=True)[0]
    note = root / 'knowledge/notes/project-rule.md'
    note.write_bytes(note.read_bytes() + b'\nAdditional required context.\n')
    refreshed = compiler._refresh_compile_batch(batch)
    assert refreshed.packing.max_input_tokens > batch.packing.max_input_tokens
    assert refreshed.packing.max_input_tokens <= basis.planning_window
    assert refreshed.planning_candidates is batch.planning_candidates
    assert refreshed.required_context_paths == batch.required_context_paths
    assert note.read_bytes() in tuple(source.content for source in refreshed.inputs.sources)
