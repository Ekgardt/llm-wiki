"""Real source/binding/behavior probes; this integration packet keeps both sources."""
from pathlib import Path

import answer_budget
import answer_cost
import pytest

from tests.module_context import context_reader
from tests.module_contract_calls import require_public_calls
from tests.module_contract_surface import require_declared_surface

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/contract-identity-context.json')


def test_actual_consumer_uses_only_checked_public_bindings():
    interface = read_source(ROOT / 'docs/modules/answer-budget-interface.pyi')
    require_declared_surface(read_source(ROOT / 'scripts/answer_budget.py'), interface)
    assert require_public_calls(
        read_source(ROOT / 'scripts/answer_cost.py'), 'answer_budget', interface,
    ) == {'BYTES_PER_TOKEN': 'BYTES_PER_TOKEN', 'estimate_tokens': 'estimate_tokens'}


def test_runtime_binding_is_the_actual_checkout_provider():
    assert Path(answer_budget.__file__).resolve() == ROOT / 'scripts/answer_budget.py'
    assert Path(answer_cost.__file__).resolve() == ROOT / 'scripts/answer_cost.py'
    assert answer_cost.estimate_tokens is answer_budget.estimate_tokens
    assert answer_cost.BYTES_PER_TOKEN == answer_budget.BYTES_PER_TOKEN == 2


@pytest.mark.parametrize(('value', 'tokens'), [('', 1), ('x', 2), ('Ж', 2), ('😀', 3), ({}, 1)])
def test_real_consumer_and_provider_agree_with_utf8_contract(value, tokens):
    assert answer_budget.estimate_tokens(value) == tokens
    assert answer_cost.block_tokens(value) == tokens


def test_real_serialization_failure_is_reported_as_unknown():
    envelope = {'components': {}, 'data': {object(): 'invalid JSON key'}}
    with pytest.raises(TypeError):
        answer_budget.estimate_tokens(envelope)
    assert answer_cost.build_cost_block(
        envelope, elapsed_seconds=0.1, budget_seconds=1.0,
    )['tokens_estimated'] is None


def test_real_circular_value_is_reported_as_unknown():
    envelope = {'components': {}, 'data': {}}
    envelope['data']['cycle'] = envelope
    with pytest.raises(ValueError, match='Circular reference'):
        answer_budget.estimate_tokens(envelope)
    assert answer_cost.build_cost_block(
        envelope, elapsed_seconds=0.1, budget_seconds=1.0,
    )['tokens_estimated'] is None
