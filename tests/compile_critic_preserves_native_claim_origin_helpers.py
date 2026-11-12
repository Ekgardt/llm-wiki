"""Helpers shared by test_compile_critic_preserves_native_claim_origin.py and the test files importing them."""

from llm_wiki import compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_claims_producer_helpers import _candidate, _operation
from tests.native_compile_projection_has_complete_citation_address_helpers import _projected_inputs


def _native_operation(tmp_path, monkeypatch):
    inputs = _projected_inputs(tmp_path, monkeypatch, 'The bicycle is red.\nThe lease expires in 30 seconds.')
    choices = compile_pipeline_prompts.source_line_choices(inputs)
    operation = _operation([_candidate(evidence_index=1)])
    operation['evidence'] = [compile_pipeline_prompts.expand_choice_fields(
        {'source_line': row['source_line'], 'claim': row['quoted_text']}, choices) for row in choices]
    compile_pipeline_operations.with_derived_claims([operation], inputs)
    return inputs, operation
