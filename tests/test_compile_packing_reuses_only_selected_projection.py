from dataclasses import replace

import compile_memory as compiler
import pytest
from reliable_memory import sha256_bytes

from tests.test_native_compile_uses_whole_container import (
    _frame_covering_keys,
    _native_inputs,
    _native_operation,
)


def _setup(root, monkeypatch):
    inputs, _ = _native_inputs(root, monkeypatch, "First.\nActual observation.")
    context = compiler.SourceSnapshot("knowledge/notes/context.md", b"context", sha256_bytes(b"context"))
    return replace(inputs, sources=(*inputs.sources, context))


def _observe(monkeypatch):
    seen = []
    original = compiler._native_unit_source

    def forwarded(parts, **kwargs):
        seen.append(tuple(id(part) for part in parts))
        return original(parts, **kwargs)

    monkeypatch.setattr(compiler, "_native_unit_source", forwarded)
    return seen


def test_optional_context_reuses_one_verified_projection(tmp_path, monkeypatch):
    inputs = _setup(tmp_path, monkeypatch)
    keys = _frame_covering_keys(inputs, inputs.dailies[0].original_content)
    expected = [len(compiler._draft_prompt_text(compiler._subset_compile_inputs(inputs, keys, paths)).encode())
                for paths in (set(), {"knowledge/notes/context.md"}, set())]
    seen = _observe(monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    actual = [measure(keys, paths) for paths in (set(), {"knowledge/notes/context.md"}, set())]
    assert actual == expected
    assert len(seen) == 1


def test_changed_selection_replaces_the_projection(tmp_path, monkeypatch):
    inputs = _setup(tmp_path, monkeypatch)
    plain = compiler._daily_parts("knowledge/daily/2026-10-03.md", b"# Day\nPlain fact.\n")[0]
    inputs = replace(inputs, dailies=(*inputs.dailies, plain))
    native = _frame_covering_keys(inputs, inputs.dailies[0].original_content)
    seen = _observe(monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    for keys in (native, {plain.part_key}, native):
        assert measure(keys) == len(compiler._draft_prompt_text(compiler._subset_compile_inputs(inputs, keys)).encode())
    assert len(seen) == 6  # Three independent expected requests plus three selected projections.


def test_replaced_same_key_forged_frame_is_not_cached(tmp_path, monkeypatch):
    inputs = _setup(tmp_path, monkeypatch)
    keys = _frame_covering_keys(inputs, inputs.dailies[0].original_content)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys)
    frame = replace(inputs.dailies[0].native_frames[0], text="Forged.")
    for key in keys:
        measure.dailies[key] = [replace(part, native_frames=(frame,)) for part in measure.dailies[key]]
    with pytest.raises(ValueError, match="native frame cache"):
        measure(keys)


def test_final_subset_and_binding_recheck_changed_permanent_head(tmp_path, monkeypatch):
    inputs = _setup(tmp_path, monkeypatch)
    keys = _frame_covering_keys(inputs, inputs.dailies[0].original_content)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys)
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.write_bytes(head.read_bytes() + b"tampered\n")
    with pytest.raises(ValueError):
        compiler._compile_batch(inputs, keys, compiler._compile_budget(None), None, None)
    operation = _native_operation(inputs, "Actual observation.")
    with pytest.raises(ValueError):
        compiler._evidence_binding(operation["evidence"][0], inputs)
