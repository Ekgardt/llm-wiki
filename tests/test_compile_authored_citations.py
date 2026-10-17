"""New model prose must carry only complete, operation-owned citations."""
import json

import pytest

from tests import test_compile_transactions as transactions
from tests.test_compile_transactions import _daily, _semantic_plan

vault = transactions.vault


def prepared(vault):
    import compile_memory as compiler

    root, _state = vault
    daily = _daily(root)
    inputs = compiler.snapshot_compile_inputs([daily])
    operation = json.loads(_semantic_plan()['operations'][0]['content'])
    validated, bindings = compiler._validate_semantic_operation(operation, inputs)
    return compiler, inputs, validated, bindings[0]['reference']


@pytest.mark.parametrize('field', ['title', 'summary', 'body_markdown'])
def test_short_authored_reference_is_rejected(vault, field):
    compiler, inputs, operation, _reference = prepared(vault)
    operation[field] = 'Observation daily:2026-07-14 10:00:00'
    with pytest.raises(ValueError):
        compiler._validate_semantic_operation(operation, inputs)


def test_short_reference_in_authored_claim_is_rejected(vault):
    compiler, inputs, operation, _reference = prepared(vault)
    operation['evidence'][0]['claim'] = 'Observed daily:2026-07-14 10:00:00'
    with pytest.raises(ValueError):
        compiler._validate_semantic_operation(operation, inputs)


def test_exact_bound_reference_is_accepted_and_rendered(vault):
    compiler, inputs, operation, reference = prepared(vault)
    operation['body_markdown'] = f'Proven observation: `{reference}`.'
    validated, bindings = compiler._validate_semantic_operation(operation, inputs)
    assert validated['body_markdown'] == operation['body_markdown']
    rendered = compiler._render_page(validated, '2026-07-14T12:00:00Z', [reference]).decode()
    assert reference in rendered
    assert bindings[0]['reference'] == reference


def test_complete_unowned_reference_is_rejected(vault):
    compiler, inputs, operation, reference = prepared(vault)
    operation['body_markdown'] = reference.replace('block:10:00:00', 'block:11:00:00')
    with pytest.raises(ValueError, match='authored citation is not bound'):
        compiler._validate_semantic_operation(operation, inputs)


def test_plain_prose_without_reference_is_unchanged(vault):
    compiler, inputs, operation, _reference = prepared(vault)
    validated, _bindings = compiler._validate_semantic_operation(operation, inputs)
    assert validated == operation
