"""Optional context selection preserves exact bytes without rescanning the daily."""
from dataclasses import replace

import compile_receipts
import pytest
from context_budget import ContextBudget
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from reliable_memory import sha256_bytes


def _source(path, content):
    return compile_receipts.SourceSnapshot(path, content, sha256_bytes(content))


def _inputs():
    daily = compile_pipeline_sources.daily_parts("knowledge/daily/2026-10-02.md", b"# Day\nA durable fact.\n")[0]
    notes = tuple(_source(f"knowledge/notes/{number}.md", b"context" * number)
                  for number in range(1, 9))
    return compile_pipeline_sources.CompileInputs((daily,), notes, ()), notes


def _legacy_selection(paths, notes, budget, measure):
    chosen = set()
    for note in notes:
        candidate = chosen | {note.logical_path}
        if measure(paths, candidate) <= budget.available_input_tokens:
            chosen = candidate
    return chosen


def test_optional_selection_crosses_the_daily_measure_once(monkeypatch):
    inputs, notes = _inputs()
    paths = {inputs.dailies[0].part_key}
    calls = []
    original = compile_pipeline_prompts.ByteBatchMeasure.__call__

    def observe(self, *args):
        calls.append(args)
        return original(self, *args)

    monkeypatch.setattr(compile_pipeline_prompts.ByteBatchMeasure, "__call__", observe)
    selected = compile_pipeline_batches.fitting_context(paths, notes, compile_pipeline_base.compile_budget(None),
                                         compile_pipeline_prompts.ByteBatchMeasure(inputs))
    assert selected == {note.logical_path for note in notes}
    assert len(calls) == 1


@pytest.mark.parametrize("empty_daily", [False, True])
@pytest.mark.parametrize("duplicate", [False, True])
def test_selected_context_matches_exact_existing_serialization(empty_daily, duplicate):
    inputs, notes = _inputs()
    notes = notes + (notes[0],) if duplicate else notes
    inputs = replace(inputs, sources=notes, dailies=() if empty_daily else inputs.dailies)
    paths = {part.part_key for part in inputs.dailies}
    base = compile_pipeline_prompts.ByteBatchMeasure(inputs)(paths)
    budget = ContextBudget(None, base + 100, 0, 0)
    expected = _legacy_selection(paths, notes, budget, compile_pipeline_prompts.ByteBatchMeasure(inputs))
    actual = compile_pipeline_batches.fitting_context(paths, notes, budget, compile_pipeline_prompts.ByteBatchMeasure(inputs))
    assert actual == expected
    subset = compile_pipeline_sources.subset_compile_inputs(inputs, paths, actual)
    assert compile_pipeline_prompts.ByteBatchMeasure(inputs)(paths, actual) == len(compile_pipeline_prompts.draft_prompt_text(subset).encode())
    assert compile_pipeline_prompts.ByteBatchMeasure(inputs)(paths, actual) <= budget.available_input_tokens


def test_nonadditive_tokenizer_keeps_the_complete_measurement():
    inputs, notes = _inputs()
    paths = {inputs.dailies[0].part_key}
    calls = []

    def measured(selected, optional):
        calls.append((selected, optional))
        return 5 + len(optional) ** 2

    budget = ContextBudget(None, 14, 0, 0)
    actual = compile_pipeline_batches.fitting_context(paths, notes, budget, measured)
    assert len(actual) == 3
    assert len(calls) == len(notes)
