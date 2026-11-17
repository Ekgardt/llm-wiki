"""Helpers shared by test_compile_groups_complete_sources_by_qualified_capacity.py and the test files importing them."""

from types import SimpleNamespace

import compile_receipts
from llm_wiki import (
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from reliable_memory import sha256_bytes

MODEL = 'qualified-attempt-model'


def _inputs():
    target = compile_pipeline_base.compile_budget(MODEL)
    empty = len(compile_pipeline_prompts.draft_prompt_text(compile_pipeline_sources.CompileInputs((), (), ())).encode())
    body = b'x' * ((target.available_input_tokens - empty) * 2 // 3)
    snapshots = tuple(_day(day, body) for day in ('2026-10-01', '2026-10-02'))
    parts = tuple(part for path, content in snapshots for part in compile_pipeline_sources.daily_parts(path, content))
    sources = tuple(compile_receipts.SourceSnapshot(path, content, sha256_bytes(content)) for path, content in snapshots)
    return compile_pipeline_sources.CompileInputs(parts, sources, ())


def _day(day, body):
    return f'knowledge/daily/{day}.md', b'## [10:00:00] session-end | manual\n' + body + b'\n'


def _minimum(inputs):
    target = compile_pipeline_base.compile_budget(MODEL)
    return compile_pipeline_batches.draft_prompt_count(inputs, MODEL, None).tokens + target.reserved_output_tokens + target.safety_margin_tokens


def _candidates(window, *, provider='codex', model=MODEL):
    basis = SimpleNamespace(model=model, planning_window=window)
    return (SimpleNamespace(provider=provider, model=model, _codex_basis=basis),)
