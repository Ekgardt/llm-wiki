"""Repeated sizing reuses pure bindings; protection and publication stay fresh."""
from dataclasses import replace

import compile_memory as compiler
import pytest

from tests.test_compile_binds_the_protected_source_view import _legacy_packet, _policy
from tests.test_compile_reuses_raw_row_positions_within_one_layout import _context_pages


def _observed_bindings(monkeypatch):
    calls = []
    original = compiler._bound_legacy_evidence

    def observed(*args):
        calls.append(args[:3])
        return original(*args)

    monkeypatch.setattr(compiler, '_bound_legacy_evidence', observed)
    return calls


def _with_context(inputs, text):
    raw = text.encode()
    note = compiler.SourceSnapshot('knowledge/notes/context.md', raw, compiler.sha256_bytes(raw))
    return replace(inputs, sources=(*inputs.sources, note))


def test_unchanged_daily_binding_is_not_recomputed_for_each_optional_page(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    calls = _observed_bindings(monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    with compiler._measure_choice_resolution(measure):
        first = compiler._draft_layout(_with_context(inputs, 'First full context.'))
        second = compiler._draft_layout(_with_context(inputs, 'Second full context.'))
    assert first != second
    assert calls.count(('2026-09-25', '13:51:26', 'A complete durable fact.')) == 1
    assert 'Second full context.' in second[0]
    compiler._draft_layout(inputs)
    assert calls.count(('2026-09-25', '13:51:26', 'A complete durable fact.')) == 2


def test_reused_sizing_never_accepts_changed_member_hash(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    measure(keys)
    part = inputs.dailies[0]
    measure.dailies[part.part_key] = [replace(part, sha256='0' * 64)]
    with pytest.raises(ValueError):
        measure(keys)


def test_policy_changes_are_applied_to_each_fresh_layout(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['The label is private-word.'])
    measure = compiler._ByteBatchMeasure(inputs)
    with compiler._measure_choice_resolution(measure):
        before = compiler._draft_layout(inputs)
        _policy(monkeypatch, tmp_path)
        after = compiler._draft_layout(inputs)
    assert 'private-word' in before[0]
    assert 'private-word' not in after[0]
    assert '[REDACTED_LITERAL]' in after[0]


def _uncached_binding(item, inputs, proof):
    return compiler._evidence_binding(item, inputs)


def _packing_result(inputs):
    batches = compiler.pack_compile_batches(inputs, model=None)
    return batches, tuple(compiler._draft_layout(batch.inputs) for batch in batches)


@pytest.mark.parametrize('rows', (100, 750))
def test_entire_packing_layout_and_schema_match_uncached_baseline(tmp_path, monkeypatch, record_property, rows):
    import time

    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(rows)])
    parts = tuple(compiler._daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    inputs = replace(inputs, dailies=parts, sources=(*inputs.sources, *_context_pages()))
    current = compiler._measured_original_choice_binding
    monkeypatch.setattr(compiler, '_measured_original_choice_binding', _uncached_binding)
    wall, cpu = time.monotonic(), time.process_time()
    baseline = _packing_result(inputs)
    record_property('baseline_wall', time.monotonic() - wall)
    record_property('baseline_cpu', time.process_time() - cpu)
    monkeypatch.setattr(compiler, '_measured_original_choice_binding', current)
    wall, cpu = time.monotonic(), time.process_time()
    actual = _packing_result(inputs)
    record_property('candidate_wall', time.monotonic() - wall)
    record_property('candidate_cpu', time.process_time() - cpu)
    assert actual == baseline
    assert actual[0]
    for batch in actual[0]:
        assert {item.logical_path for item in batch.inputs.sources} == {item.logical_path for item in inputs.sources}


def test_measurement_binding_scope_restores_after_exception(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A complete durable fact.'])
    with pytest.raises(RuntimeError), compiler._measure_choice_resolution(compiler._ByteBatchMeasure(inputs)):
        compiler._draft_layout(inputs)
        raise RuntimeError('stop sizing')
    assert compiler._SOURCE_CHOICE_BINDINGS.get() is None
