"""Packing must not repeatedly render the immutable schema or scan unrelated days."""
import compile_memory as cm
import pytest


def _inputs():
    day = cm.DailySnapshot('knowledge/daily/день.md', 'факт\n'.encode(), 'd')
    note = cm.SourceSnapshot('knowledge/notes/頁.md', '知識\n'.encode(), 'n')
    return cm.CompileInputs((day,), (note,), ())


def test_byte_measure_reuses_schema(monkeypatch):
    inputs = _inputs()
    calls = []
    original = cm.canonical_json_bytes

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(cm, 'canonical_json_bytes', counted)
    measure = cm._batch_measure(inputs, None, None)
    paths = {inputs.dailies[0].part_key}
    expected = cm.count_tokens(cm._draft_prompt_text(cm._subset_compile_inputs(inputs, paths))).tokens
    calls.clear()
    assert measure(paths) == expected
    assert measure(paths) == expected
    assert not calls


@pytest.mark.parametrize('optional', [set(), {'knowledge/notes/頁.md'}, {'absent'}])
def test_byte_measure_equals_complete_prompt(optional):
    inputs = _inputs()
    paths = {inputs.dailies[0].part_key, 'absent'}
    subset = cm._subset_compile_inputs(inputs, paths, optional)
    assert cm._batch_measure(inputs, None, None)(paths, optional) == cm.count_tokens(cm._draft_prompt_text(subset)).tokens


def test_model_adapter_counts_the_whole_prompt():
    inputs = _inputs()
    paths = {inputs.dailies[0].part_key}
    def adapter(text):
        return len(text.split())
    expected = adapter(cm._draft_prompt_text(cm._subset_compile_inputs(inputs, paths)))
    assert cm._batch_measure(inputs, 'explicit', {'explicit': adapter})(paths) == expected


def test_duplicate_daily_parts_are_still_refused():
    inputs = _inputs()
    duplicated = cm.CompileInputs(inputs.dailies * 2, inputs.sources, ())
    measure = cm._batch_measure(duplicated, None, None)
    with pytest.raises(ValueError, match='two parts'):
        measure({inputs.dailies[0].part_key})


def test_duplicate_context_frames_are_preserved():
    inputs = _inputs()
    duplicated = cm.CompileInputs(inputs.dailies, inputs.sources * 2, ())
    paths = {inputs.dailies[0].part_key}
    optional = {inputs.sources[0].logical_path}
    expected = cm.count_tokens(cm._draft_prompt_text(cm._subset_compile_inputs(duplicated, paths, optional))).tokens
    assert cm._batch_measure(duplicated, None, None)(paths, optional) == expected


def test_unselected_invalid_utf8_is_not_read():
    inputs = _inputs()
    bad = cm.SourceSnapshot('knowledge/notes/bad.md', b'\xff', 'b')
    with_bad = cm.CompileInputs(inputs.dailies, (*inputs.sources, bad), ())
    measure = cm._batch_measure(with_bad, None, None)
    assert measure(set()) == cm.count_tokens(cm._draft_prompt_text(cm.CompileInputs((), (), ()))).tokens
    with pytest.raises(UnicodeDecodeError):
        measure(set(), {bad.logical_path})


def test_empty_selection_equals_empty_prompt():
    inputs = _inputs()
    assert cm._batch_measure(inputs, None, None)(set()) == cm.count_tokens(cm._draft_prompt_text(cm.CompileInputs((), (), ()))).tokens
