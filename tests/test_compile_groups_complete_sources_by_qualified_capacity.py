"""Complete mandatory source groups use qualified capacity, not optional fill space."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as cm
import pytest
from reliable_memory import sha256_bytes

from tests.test_compile_keeps_physically_overlapping_existing_context import _large_required
from tests.test_compile_keeps_physically_overlapping_existing_context import vault as vault
from tests.test_native_compile_uses_whole_container import _native_inputs

MODEL = 'qualified-attempt-model'


def _inputs():
    target = cm._compile_budget(MODEL)
    empty = len(cm._draft_prompt_text(cm.CompileInputs((), (), ())).encode())
    body = b'x' * ((target.available_input_tokens - empty) * 2 // 3)
    snapshots = tuple(_day(day, body) for day in ('2026-10-01', '2026-10-02'))
    parts = tuple(part for path, content in snapshots for part in cm._daily_parts(path, content))
    sources = tuple(cm.SourceSnapshot(path, content, sha256_bytes(content)) for path, content in snapshots)
    return cm.CompileInputs(parts, sources, ())


def _day(day, body):
    return f'knowledge/daily/{day}.md', b'## [10:00:00] session-end | manual\n' + body + b'\n'


def _minimum(inputs):
    target = cm._compile_budget(MODEL)
    return cm._draft_prompt_count(inputs, MODEL, None).tokens + target.reserved_output_tokens + target.safety_margin_tokens


def _candidates(window, *, provider='codex', model=MODEL):
    basis = SimpleNamespace(model=model, planning_window=window)
    return (SimpleNamespace(provider=provider, model=model, _codex_basis=basis),)


def test_qualified_capacity_joins_complete_source_bytes_without_optional_inflation():
    inputs = _inputs()
    baseline = cm.pack_compile_batches(inputs, model=MODEL)
    assert len(baseline) == 2
    mandatory = _minimum(inputs)
    assert mandatory > cm._compile_budget(MODEL).max_input_tokens
    optional = cm.SourceSnapshot('knowledge/notes/optional.md', b'z' * mandatory, sha256_bytes(b'z' * mandatory))
    augmented = cm.CompileInputs(inputs.dailies, (*inputs.sources, optional), inputs.targets)
    candidates = _candidates(mandatory + len(optional.content))
    batches = cm.pack_compile_batches(augmented, model=MODEL, planning_candidates=candidates)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.inputs.dailies == inputs.dailies
    assert batch.inputs.sources == inputs.sources
    assert batch.manifest == tuple(sorted(cm._source_descriptor(part) for part in inputs.dailies))
    assert batch.packing.max_input_tokens == mandatory
    assert batch.packing.measured_input_tokens == len(cm._draft_prompt_text(inputs).encode())
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
    batches = cm.pack_compile_batches(inputs, model=MODEL, planning_candidates=choices[candidate_kind])
    assert len(batches) == 2
    assert tuple(part for batch in batches for part in batch.inputs.dailies) == inputs.dailies


def test_one_byte_below_complete_group_capacity_splits_without_dropping_sources():
    inputs = _inputs()
    batches = cm.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(inputs) - 1))
    assert len(batches) == 2
    assert tuple(part for batch in batches for part in batch.inputs.dailies) == inputs.dailies


def test_provisional_native_group_remains_unready_for_dispatch():
    inputs = _inputs()
    candidates = _candidates(_minimum(inputs))
    batches = cm._pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates, context_pending=True)
    assert len(batches) == 1
    assert batches[0].context_pending
    with pytest.raises(ValueError, match='context.*refresh'):
        cm._require_ready_compile_batch(batches[0])


def test_group_retains_the_entire_physically_required_decision(vault):
    root, _state = vault
    initial = _large_required(root)
    path, raw = _day('2026-10-02', b'Second independent scoped source fact.')
    (root / path).write_bytes(raw)
    inputs = cm.snapshot_compile_inputs([root / initial.dailies[0].logical_path, root / path])
    paths = {part.part_key for part in inputs.dailies}
    mandatory = cm._subset_compile_inputs(inputs, paths, {'knowledge/notes/project-rule.md'})
    candidates = _candidates(_minimum(mandatory))
    batches = cm.pack_compile_batches(inputs, model=MODEL, planning_candidates=candidates)
    assert len(batches) == 1
    assert batches[0].inputs.sources == mandatory.sources
    assert batches[0].inputs.dailies == inputs.dailies
    assert batches[0].required_context_paths == ('knowledge/notes/project-rule.md',)
    assert batches[0].packing.max_input_tokens == _minimum(mandatory)


def test_qualified_group_preserves_the_complete_native_frame_and_physical_source(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    assert len(inputs.dailies) > 1
    paths = {part.part_key for part in inputs.dailies}
    whole = cm._subset_compile_inputs(inputs, paths)
    batches = cm.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(whole)))
    assert len(batches) == 1
    assert batches[0].inputs.dailies == inputs.dailies
    assert batches[0].inputs.sources == whole.sources
    assert whole.sources[0].content == inputs.dailies[0].original_content


def test_qualified_capacity_cannot_authorize_a_missing_native_companion(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 2000)
    assert len(inputs.dailies) > 2
    whole = cm._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
    incomplete = replace(inputs, dailies=(inputs.dailies[0], inputs.dailies[-1]))
    with pytest.raises(ValueError, match='cover|native|complete|source|join'):
        cm.pack_compile_batches(incomplete, model=MODEL, planning_candidates=_candidates(_minimum(whole)))


def test_native_atomic_unit_cannot_exceed_the_qualified_model_window(tmp_path, monkeypatch):
    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A complete native source. ' * 1000)
    unit = max(cm._native_part_units(inputs.dailies), key=len)
    assert len(unit) > 1
    complete_unit = cm._subset_compile_inputs(inputs, {part.part_key for part in unit})
    with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
        cm.pack_compile_batches(inputs, model=MODEL, planning_candidates=_candidates(_minimum(complete_unit) - 1))
