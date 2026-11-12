"""The planned budget includes the complete serialized original-entry context."""
from dataclasses import replace

import compile_receipts
import pytest
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from reliable_memory import sha256_bytes

from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("optional", [False, True])
def test_planned_bytes_equal_the_complete_serialized_request(legacy, optional):
    inputs = _inputs(legacy, optional)
    keys = {inputs.dailies[0].part_key}
    context = {"knowledge/notes/évidence.md"} if optional else set()
    selected = compile_pipeline_sources.subset_compile_inputs(inputs, keys, context)
    estimate = compile_pipeline_prompts.ByteBatchMeasure(inputs)(keys, context)
    assert estimate == len(compile_pipeline_prompts.draft_prompt_text(selected).encode("utf-8"))


def test_original_context_prevents_a_one_byte_oversized_optional_request():
    inputs = _inputs()
    budget = compile_pipeline_base.compile_budget(None)
    path = "knowledge/notes/optional.md"
    empty = compile_receipts.SourceSnapshot(path, b"", sha256_bytes(b""))
    zero_context = replace(inputs, sources=(*inputs.sources, empty))
    empty_bytes = len(compile_pipeline_prompts.draft_prompt_text(zero_context).encode())
    body = b"x" * (budget.available_input_tokens - empty_bytes + 1)
    note = compile_receipts.SourceSnapshot(path, body, sha256_bytes(body))
    overflowing = replace(inputs, sources=(*inputs.sources, note))
    assert len(compile_pipeline_prompts.draft_prompt_text(overflowing).encode()) == budget.available_input_tokens + 1
    batches = compile_pipeline_batches.pack_compile_batches(overflowing, model=None)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.inputs.dailies == inputs.dailies
    assert path not in {source.logical_path for source in batch.inputs.sources}
    assert batch.packing.measured_input_tokens == len(compile_pipeline_prompts.draft_prompt_text(batch.inputs).encode())
    assert batch.packing.measured_input_tokens <= budget.available_input_tokens
