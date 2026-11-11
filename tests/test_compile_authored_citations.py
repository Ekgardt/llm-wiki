"""New model prose must carry only complete, operation-owned citations."""
import json

import pytest
from llm_wiki import compile_pipeline_claims, compile_pipeline_prompts, compile_pipeline_sources

from tests import compile_transactions_helpers as transactions_helpers  # noqa: E402
from tests.compile_transactions_helpers import _daily, _semantic_plan

vault = transactions_helpers.vault


def prepared(vault):
    import compile_memory as compiler
    from llm_wiki import compile_pipeline_prompts

    root, _state = vault
    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operation = json.loads(_semantic_plan()['operations'][0]['content'])
    validated, bindings = compile_pipeline_prompts.validate_semantic_operation(operation, inputs)
    return compiler, inputs, validated, bindings[0]['reference']


@pytest.mark.parametrize('field', ['title', 'summary', 'body_markdown'])
def test_short_authored_reference_is_rejected(vault, field):
    compiler, inputs, operation, _reference = prepared(vault)
    operation[field] = 'Observation daily:2026-07-14 10:00:00'
    with pytest.raises(ValueError):
        compile_pipeline_prompts.validate_semantic_operation(operation, inputs)


def test_short_reference_in_authored_claim_is_rejected(vault):
    compiler, inputs, operation, _reference = prepared(vault)
    operation['evidence'][0]['claim'] = 'Observed daily:2026-07-14 10:00:00'
    with pytest.raises(ValueError):
        compile_pipeline_prompts.validate_semantic_operation(operation, inputs)


def test_exact_bound_reference_is_accepted_and_rendered(vault):
    compiler, inputs, operation, reference = prepared(vault)
    operation['body_markdown'] = f'Proven observation: `{reference}`.'
    validated, bindings = compile_pipeline_prompts.validate_semantic_operation(operation, inputs)
    assert validated['body_markdown'] == operation['body_markdown']
    rendered = compile_pipeline_claims.render_page(validated, '2026-07-14T12:00:00Z', [reference]).decode()
    assert reference in rendered
    assert bindings[0]['reference'] == reference


def test_complete_unowned_reference_is_rejected(vault):
    compiler, inputs, operation, reference = prepared(vault)
    operation['body_markdown'] = reference.replace('block:10:00:00', 'block:11:00:00')
    with pytest.raises(ValueError, match='authored citation is not bound'):
        compile_pipeline_prompts.validate_semantic_operation(operation, inputs)


def test_plain_prose_without_reference_is_unchanged(vault):
    compiler, inputs, operation, _reference = prepared(vault)
    validated, _bindings = compile_pipeline_prompts.validate_semantic_operation(operation, inputs)
    assert validated == operation
