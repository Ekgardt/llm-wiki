"""Fresh context can change measured size without changing declared capacity."""
from dataclasses import replace

import compile_pipeline
import compile_receipts
from llm_wiki import compile_pipeline_base, compile_pipeline_batches, compile_pipeline_sources

from tests.codex_planning_basis_owns_the_selected_model_helpers import _basis
from tests.compile_claims_producer_helpers import vault as vault
from tests.compile_keeps_physically_overlapping_existing_context_helpers import _large_required
from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path


def test_declared_budget_does_not_shrink_when_mandatory_input_is_smaller(tmp_path):
    basis = _basis(tmp_path)
    target = compile_pipeline_base.compile_budget(basis.model)
    actual = compile_pipeline_batches.complete_source_budget(target, 1000, set(), (basis.descriptor,))
    assert actual == target


def test_refresh_preserves_capacity_with_new_complete_context(tmp_path, monkeypatch):
    basis = _basis(tmp_path)
    batch = compile_pipeline_batches.pack_planned_compile_batches(_inputs(), model=basis.model,
        planning_candidates=(basis.descriptor,), context_pending=True)[0]
    raw = b'A fresh context page.\n'
    note = compile_receipts.SourceSnapshot('knowledge/notes/fresh-context.md', raw, compile_pipeline.sha256_bytes(raw))
    target = compile_pipeline_sources.TargetSnapshot(note.logical_path, raw, note.sha256)
    context = compile_pipeline_sources.CompileInputs((), (note,), (target,))
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', lambda paths: context)
    refreshed = compile_pipeline_batches.refresh_compile_batch(batch)
    assert refreshed.packing.max_input_tokens == batch.packing.max_input_tokens
    assert refreshed.packing.measured_input_tokens != batch.packing.measured_input_tokens
    assert note in refreshed.inputs.sources
    assert refreshed.inputs.targets == (target,)


def test_capacity_still_expands_only_for_complete_required_input(tmp_path):
    basis = _basis(tmp_path)
    target = compile_pipeline_base.compile_budget(basis.model)
    measured = target.max_input_tokens + 1000
    expected = measured + target.reserved_output_tokens + target.safety_margin_tokens
    actual = compile_pipeline_batches.complete_source_budget(target, measured, set(), (basis.descriptor,))
    assert actual == replace(target, max_input_tokens=expected)
    limited = replace(basis, planning_window=expected - 1)
    assert compile_pipeline_batches.complete_source_budget(target, measured, set(), (limited.descriptor,)) == target


def test_new_required_context_replans_measured_capacity_under_same_provider(vault, tmp_path):
    root, _state = vault
    inputs = _large_required(root)
    basis = _basis(tmp_path)
    batch = compile_pipeline_batches.pack_planned_compile_batches(inputs, model=basis.model,
        planning_candidates=(basis.descriptor,), context_pending=True)[0]
    note = root / 'knowledge/notes/project-rule.md'
    note.write_bytes(note.read_bytes() + b'\nAdditional required context.\n')
    refreshed = compile_pipeline_batches.refresh_compile_batch(batch)
    assert refreshed.packing.max_input_tokens > batch.packing.max_input_tokens
    assert refreshed.packing.max_input_tokens <= basis.planning_window
    assert refreshed.planning_candidates is batch.planning_candidates
    assert refreshed.required_context_paths == batch.required_context_paths
    assert note.read_bytes() in tuple(source.content for source in refreshed.inputs.sources)
