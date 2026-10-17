"""Optional context must not repeatedly derive the same complete source choices."""
from dataclasses import replace

import compile_memory as compiler
import pytest

from tests.test_compile_binds_the_protected_source_view import _legacy_packet, _policy
from tests.test_compile_critic_preserves_native_claim_origin import _native_operation


def _context_inputs(root, monkeypatch):
    inputs = _legacy_packet(root, monkeypatch, [f'Original durable fact {i}.' for i in range(20)])
    notes = tuple(compiler.SourceSnapshot(
        f'knowledge/notes/context-{i}.md', f'Independent context {i}.\n'.encode(),
        compiler.sha256_bytes(f'Independent context {i}.\n'.encode()),
    ) for i in range(3))
    return replace(inputs, sources=(*inputs.sources, *notes)), notes


def test_different_optional_context_reuses_only_identical_verified_choices(tmp_path, monkeypatch):
    inputs, notes = _context_inputs(tmp_path, monkeypatch)
    paths = {part.part_key for part in inputs.dailies}
    expected = [len(compiler._draft_prompt_text(
        compiler._subset_compile_inputs(inputs, paths, {note.logical_path})
    ).encode()) for note in notes]
    calls = []
    original = compiler._collect_choice_pair

    def observed(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(compiler, '_collect_choice_pair', observed)
    measure = compiler._ByteBatchMeasure(inputs)
    assert [measure(paths, {note.logical_path}) for note in notes] == expected
    assert len(calls) == len(inputs.dailies[0].content.splitlines())
    assert compiler._SOURCE_CHOICE_BINDINGS.get() is None


def test_unscoped_layouts_still_derive_choices_independently(tmp_path, monkeypatch):
    inputs, notes = _context_inputs(tmp_path, monkeypatch)
    paths = {part.part_key for part in inputs.dailies}
    first = compiler._subset_compile_inputs(inputs, paths, {notes[0].logical_path})
    choices = compiler._source_line_choices(first)
    choices[0]['quoted_text'] = 'A caller mutation must not survive.'
    assert compiler._source_line_choices(first)[0]['quoted_text'] != choices[0]['quoted_text']


def test_warmed_choices_still_use_the_current_protected_source_view(tmp_path, monkeypatch):
    inputs, _notes = _context_inputs(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    with compiler._measure_choice_resolution(measure):
        original = compiler._source_line_choices(inputs)
        _policy(monkeypatch, tmp_path, literals=('Original durable fact',))
        changed = compiler._source_line_choices(inputs)
    assert changed != original
    assert 'Original durable fact' not in str(changed)


def test_warmed_choices_never_authorize_changed_source_bytes(tmp_path, monkeypatch):
    inputs, _notes = _context_inputs(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    with compiler._measure_choice_resolution(measure):
        compiler._source_line_choices(inputs)
        source = replace(inputs.sources[0], content=b'Forged source bytes.')
        changed = replace(inputs, sources=(source, *inputs.sources[1:]))
        with pytest.raises(ValueError):
            compiler._source_line_choices(changed)
    assert compiler._SOURCE_CHOICE_BINDINGS.get() is None


def test_caller_cannot_mutate_retained_native_selectors(tmp_path, monkeypatch):
    inputs, _operation = _native_operation(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    with compiler._measure_choice_resolution(measure):
        first = compiler._source_line_choices(inputs)
        expected = first[0]['native_event']['byte_start']
        first[0]['native_event']['byte_start'] = -1
        assert compiler._source_line_choices(inputs)[0]['native_event']['byte_start'] == expected


def test_equal_length_context_before_the_daily_still_changes_line_ids(tmp_path, monkeypatch):
    inputs, _notes = _context_inputs(tmp_path, monkeypatch)
    first = compiler.SourceSnapshot('knowledge/a-context.md', b'ab\ncd', compiler.sha256_bytes(b'ab\ncd'))
    second = replace(first, content=b'a\nb\nc', sha256=compiler.sha256_bytes(b'a\nb\nc'))
    before = replace(inputs, sources=(first, inputs.sources[0]))
    after = replace(inputs, sources=(second, inputs.sources[0]))
    expected = compiler._source_line_choices(after)
    measure = compiler._ByteBatchMeasure(before)
    with compiler._measure_choice_resolution(measure):
        original = compiler._source_line_choices(before)
        changed = compiler._source_line_choices(after)
    assert changed == expected
    assert changed[0]['source_line'] != original[0]['source_line']
