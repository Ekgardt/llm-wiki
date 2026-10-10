"""Choice sizing reuses pure partition parsing, never a source-validation verdict."""
from dataclasses import replace

import compile_memory as compiler
import pytest


def _inputs(tmp_path, monkeypatch):
    path = 'knowledge/daily/2026-10-02.md'
    raw = ('## [13:51:26] event\n' + 'A durable exact source fact.\n' * 900).encode()
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    monkeypatch.setattr(compiler, 'ROOT', tmp_path)
    parts = compiler._daily_parts(path, raw)
    assert len(parts) > 1
    return compiler.CompileInputs(parts, (), ())


def test_repeated_choice_layout_does_not_reparse_whole_day(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path, monkeypatch)
    calls = []
    original = compiler._partition_for_join

    def observed(part, **kwargs):
        calls.append(part.logical_path)
        return original(part, **kwargs)

    monkeypatch.setattr(compiler, '_partition_for_join', observed)
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    first = measure(keys)
    assert measure(keys) == first
    assert len(calls) == 1


def test_choice_measure_retains_member_hash_checks(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    measure(keys)
    part = inputs.dailies[0]
    measure.dailies[part.part_key] = [replace(part, sha256='0' * 64)]
    with pytest.raises(ValueError):
        measure(keys)


def test_final_projection_does_not_inherit_measurement_proofs(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    measure(keys)
    forged = replace(inputs.dailies[0], original_sha256='0' * 64)
    changed = replace(inputs, dailies=(forged, *inputs.dailies[1:]))
    with pytest.raises(ValueError):
        compiler._subset_compile_inputs(changed, keys)


def test_measured_layout_equals_independent_final_layout(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path, monkeypatch)
    keys = {part.part_key for part in inputs.dailies}
    measure = compiler._ByteBatchMeasure(inputs)
    actual = measure(keys)
    final = compiler._subset_compile_inputs(inputs, keys)
    assert actual == len(compiler._draft_prompt_text(final).encode())


def test_choice_parsing_scope_is_restored_on_failure(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    with pytest.raises(RuntimeError), compiler._measure_choice_resolution(measure):
        raise RuntimeError('stop sizing')
    assert compiler._choice_projection_parsing() == (None, None)
