"""Helpers shared by test_compile_uses_the_attempt_model_budget.py and the test files importing them."""

from dataclasses import replace

from llm_wiki import compile_pipeline_batches, compile_pipeline_prompts

from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs


def _attempt():
    inputs = _inputs()
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    packing = replace(batch.packing, max_input_tokens=90000)
    return compile_pipeline_prompts.CompileAttempt(inputs, None, replace(batch, packing=packing,
                                   planning_model="actual-attempt-model"), None)
