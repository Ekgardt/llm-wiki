"""Complete mandatory source groups use qualified capacity, not optional fill space."""
from dataclasses import replace
from types import SimpleNamespace

import compile_receipts
import pytest
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from reliable_memory import sha256_bytes

from tests.compile_claims_producer_helpers import vault as vault
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import (
    MODEL,
    _candidates,
    _day,
    _inputs,
    _minimum,
)
from tests.compile_keeps_physically_overlapping_existing_context_helpers import _large_required
from tests.native_compile_uses_whole_container_helpers import _native_inputs


def test_qualified_capacity_joins_complete_source_bytes_without_optional_inflation():
    inputs = _inputs()
    baseline = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL)
    assert len(baseline) == 2
    mandatory = _minimum(inputs)
    assert mandatory > compile_pipeline_base.compile_budget(MODEL).max_input_tokens
    optional = compile_receipts.SourceSnapshot('knowledge/notes/optional.md', b'z' * mandatory, sha256_bytes(b'z' * mandatory))
    augmented = compile_pipeline_sources.CompileInputs(inputs.dailies, (*inputs.sources, optional), inputs.targets)
    candidates = _candidates(mandatory + len(optional.content))
    batches = compile_pipeline_batches.pack_compile_batches(augmented, model=MODEL, planning_candidates=candidates)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.inputs.dailies == inputs.dailies
    assert batch.inputs.sources == inputs.sources
    assert batch.manifest == tuple(sorted(compile_pipeline_sources.source_descriptor(part) for part in inputs.dailies))
    assert batch.packing.max_input_tokens == mandatory
    assert batch.packing.measured_input_tokens == len(compile_pipeline_prompts.draft_prompt_text(inputs).encode())
    assert batch.planning_candidates is candidates


@pytest.mark.parametrize('candidate_kind', ('unknown', 'wrong_provider', 'wrong_model', 'fallbacks'))
def test_unqualified_or_fallback_capacity_keeps_existing_grouping(candidate_kind):
    inputs = _inputs()
    window = _minimum(inputs)
    choices = {
        'unknown': None,
        'wrong_provider': _candidates(window, provider='fake'),
        'wrong_model': _candidates(window, model='another-model'),
        'fallbacks': _candidates(window) * 2,
    }
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=choices[candidate_kind])
    assert len(batches) == 2
    assert tuple(part for batch in batches for part in batch.inputs.dailies) == inputs.dailies


def test_one_byte_below_complete_group_capacity_splits_without_dropping_sources():
    inputs = _inputs()
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(inputs) - 1))
    assert len(batches) == 2
    assert tuple(part for batch in batches for part in batch.inputs.dailies) == inputs.dailies


def test_provisional_native_group_remains_unready_for_dispatch():
    inputs = _inputs()
    candidates = _candidates(_minimum(inputs))
    batches = compile_pipeline_batches.pack_planned_compile_batches(inputs, model=MODEL, planning_candidates=candidates, context_pending=True)
    assert len(batches) == 1
    assert batches[0].context_pending
    with pytest.raises(ValueError, match='context.*refresh'):
        compile_pipeline_prompts.require_ready_compile_batch(batches[0])


def test_group_retains_the_entire_physically_required_decision(vault):
    root, _state = vault
    initial = _large_required(root)
    path, raw = _day('2026-10-02', b'Second independent scoped source fact.')
    (root / path).write_bytes(raw)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([root / initial.dailies[0].logical_path, root / path])
    paths = {part.part_key for part in inputs.dailies}
    mandatory = compile_pipeline_sources.subset_compile_inputs(inputs, paths, {'knowledge/notes/project-rule.md'})
    candidates = _candidates(_minimum(mandatory))
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates)
    assert len(batches) == 1
    assert batches[0].inputs.sources == mandatory.sources
    assert batches[0].inputs.dailies == inputs.dailies
    assert batches[0].required_context_paths == ('knowledge/notes/project-rule.md',)
    assert batches[0].packing.max_input_tokens == _minimum(mandatory)


def test_qualified_group_preserves_the_complete_native_frame_and_physical_source(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    assert len(inputs.dailies) > 1
    paths = {part.part_key for part in inputs.dailies}
    whole = compile_pipeline_sources.subset_compile_inputs(inputs, paths)
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(whole)))
    assert len(batches) == 1
    assert batches[0].inputs.dailies == inputs.dailies
    assert batches[0].inputs.sources == whole.sources
    assert whole.sources[0].content == inputs.dailies[0].original_content


def test_qualified_capacity_cannot_authorize_a_missing_native_companion(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 2000)
    assert len(inputs.dailies) > 2
    whole = compile_pipeline_sources.subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
    incomplete = replace(inputs, dailies=(inputs.dailies[0], inputs.dailies[-1]))
    with pytest.raises(ValueError, match='cover|native|complete|source|join'):
        compile_pipeline_batches.pack_compile_batches(incomplete, model=MODEL, planning_candidates=_candidates(_minimum(whole)))


def test_native_atomic_unit_cannot_exceed_the_qualified_model_window(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    unit = max(compile_pipeline_sources.native_part_units(inputs.dailies), key=len)
    assert len(unit) > 1
    complete_unit = compile_pipeline_sources.subset_compile_inputs(inputs, {part.part_key for part in unit})
    with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
        compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(complete_unit) - 1))


def test_automatic_provider_chain_uses_selected_models_verified_capacity():
    inputs = _inputs()
    selected = _candidates(_minimum(inputs))[0]
    fallback = SimpleNamespace(provider='claude', model='other-model', _codex_basis=None)
    unavailable = SimpleNamespace(provider='opencode', model=None, _codex_basis=None)
    candidates = (unavailable, selected, fallback)
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates)
    assert len(batches) == 1
    assert batches[0].inputs.sources == inputs.sources
    assert batches[0].inputs.dailies == inputs.dailies
    assert batches[0].planning_candidates == candidates


def test_automatic_chain_does_not_refuse_a_complete_native_source_that_fits(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    whole = compile_pipeline_sources.subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
    candidates = (*_candidates(_minimum(whole)),
                  SimpleNamespace(provider='claude', model='other-model', _codex_basis=None))
    admitted, refused = compile_pipeline_batches.partition_packable(inputs, model=MODEL, planning_candidates=candidates)
    assert not refused
    assert admitted.dailies == inputs.dailies


def test_fallback_cannot_borrow_selected_models_expanded_window():
    inputs = _inputs()
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(inputs)))[0]
    fallback = SimpleNamespace(provider='claude', model='other-model', _codex_basis=None)
    assert batch.packing.max_input_tokens > compile_pipeline_base.compile_budget(fallback.model).max_input_tokens
    bounded = compile_pipeline_prompts.attempt_input_budget(batch, fallback)
    assert bounded.max_input_tokens == compile_pipeline_base.compile_budget(fallback.model).max_input_tokens
