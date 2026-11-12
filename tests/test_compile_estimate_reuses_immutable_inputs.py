"""Packing must not repeatedly render the immutable schema or scan unrelated days."""
import compile_pipeline
import compile_receipts
import pytest
from llm_wiki import compile_pipeline_batches, compile_pipeline_prompts, compile_pipeline_sources

from tests.compile_paths import point_compile_path


def _inputs():
    day = compile_receipts.DailySnapshot('knowledge/daily/день.md', 'факт\n'.encode(), 'd')
    note = compile_receipts.SourceSnapshot('knowledge/notes/頁.md', '知識\n'.encode(), 'n')
    return compile_pipeline_sources.CompileInputs((day,), (note,), ())


def test_byte_measure_reuses_schema(monkeypatch):
    inputs = _inputs()
    calls = []
    original = compile_pipeline.canonical_json_bytes

    def counted(value):
        calls.append(value)
        return original(value)

    point_compile_path(monkeypatch, 'canonical_json_bytes', counted)
    measure = compile_pipeline_batches.batch_measure(inputs, None, None)
    paths = {inputs.dailies[0].part_key}
    expected = compile_pipeline.count_tokens(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.subset_compile_inputs(inputs, paths))).tokens
    calls.clear()
    assert measure(paths) == expected
    assert measure(paths) == expected
    assert not calls


@pytest.mark.parametrize('optional', [set(), {'knowledge/notes/頁.md'}, {'absent'}])
def test_byte_measure_equals_complete_prompt(optional):
    inputs = _inputs()
    paths = {inputs.dailies[0].part_key, 'absent'}
    subset = compile_pipeline_sources.subset_compile_inputs(inputs, paths, optional)
    assert compile_pipeline_batches.batch_measure(inputs, None, None)(paths, optional) == compile_pipeline.count_tokens(compile_pipeline_prompts.draft_prompt_text(subset)).tokens


def test_model_adapter_counts_the_whole_prompt():
    inputs = _inputs()
    paths = {inputs.dailies[0].part_key}
    def adapter(text):
        return len(text.split())
    expected = adapter(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.subset_compile_inputs(inputs, paths)))
    assert compile_pipeline_batches.batch_measure(inputs, 'explicit', {'explicit': adapter})(paths) == expected


def test_duplicate_daily_parts_are_still_refused():
    inputs = _inputs()
    parts = tuple(compile_pipeline_sources.daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    assert len(parts) == 1
    duplicated = compile_pipeline_sources.CompileInputs(parts * 2, inputs.sources, ())
    measure = compile_pipeline_batches.batch_measure(duplicated, None, None)
    with pytest.raises(ValueError, match='gap or overlap'):
        measure({parts[0].part_key})


def test_duplicate_context_frames_are_preserved():
    inputs = _inputs()
    duplicated = compile_pipeline_sources.CompileInputs(inputs.dailies, inputs.sources * 2, ())
    paths = {inputs.dailies[0].part_key}
    optional = {inputs.sources[0].logical_path}
    expected = compile_pipeline.count_tokens(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.subset_compile_inputs(duplicated, paths, optional))).tokens
    assert compile_pipeline_batches.batch_measure(duplicated, None, None)(paths, optional) == expected


def test_unselected_invalid_utf8_is_not_read():
    inputs = _inputs()
    bad = compile_receipts.SourceSnapshot('knowledge/notes/bad.md', b'\xff', 'b')
    with_bad = compile_pipeline_sources.CompileInputs(inputs.dailies, (*inputs.sources, bad), ())
    measure = compile_pipeline_batches.batch_measure(with_bad, None, None)
    assert measure(set()) == compile_pipeline.count_tokens(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.CompileInputs((), (), ()))).tokens
    with pytest.raises(UnicodeDecodeError):
        measure(set(), {bad.logical_path})


def test_empty_selection_equals_empty_prompt():
    inputs = _inputs()
    assert compile_pipeline_batches.batch_measure(inputs, None, None)(set()) == compile_pipeline.count_tokens(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.CompileInputs((), (), ()))).tokens
