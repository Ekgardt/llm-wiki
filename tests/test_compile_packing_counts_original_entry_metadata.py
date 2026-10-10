"""The planned budget includes the complete serialized original-entry context."""
from dataclasses import replace

import compile_memory as compiler
import pytest
from reliable_memory import sha256_bytes


def _inputs(legacy=False, optional=False):
    path = "knowledge/daily/2026-10-02.md"
    content = "## [10:00:00] session-end | manual\nПолный факт о велосипеде.\n".encode()
    part = compiler._daily_parts(path, content)[0]
    if legacy:
        part = replace(part, original_content=None, original_sha256="", original_entries=())
    source = compiler.SourceSnapshot(path, content, sha256_bytes(content))
    context = compiler.SourceSnapshot("knowledge/notes/évidence.md", b"context", sha256_bytes(b"context"))
    sources = (source, context) if optional else (source,)
    return compiler.CompileInputs((part,), sources, ())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("optional", [False, True])
def test_planned_bytes_equal_the_complete_serialized_request(legacy, optional):
    inputs = _inputs(legacy, optional)
    keys = {inputs.dailies[0].part_key}
    context = {"knowledge/notes/évidence.md"} if optional else set()
    selected = compiler._subset_compile_inputs(inputs, keys, context)
    estimate = compiler._ByteBatchMeasure(inputs)(keys, context)
    assert estimate == len(compiler._draft_prompt_text(selected).encode("utf-8"))


def test_original_context_prevents_a_one_byte_oversized_optional_request():
    inputs = _inputs()
    budget = compiler._compile_budget(None)
    path = "knowledge/notes/optional.md"
    empty = compiler.SourceSnapshot(path, b"", sha256_bytes(b""))
    zero_context = replace(inputs, sources=(*inputs.sources, empty))
    empty_bytes = len(compiler._draft_prompt_text(zero_context).encode())
    body = b"x" * (budget.available_input_tokens - empty_bytes + 1)
    note = compiler.SourceSnapshot(path, body, sha256_bytes(body))
    overflowing = replace(inputs, sources=(*inputs.sources, note))
    assert len(compiler._draft_prompt_text(overflowing).encode()) == budget.available_input_tokens + 1
    batches = compiler.pack_compile_batches(overflowing, model=None)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.inputs.dailies == inputs.dailies
    assert path not in {source.logical_path for source in batch.inputs.sources}
    assert batch.packing.measured_input_tokens == len(compiler._draft_prompt_text(batch.inputs).encode())
    assert batch.packing.measured_input_tokens <= budget.available_input_tokens
