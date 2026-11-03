"""Pilot input declarations are exact, contained and bound to real source owners."""
import json

import pytest

from tests.module_contract_spec import (
    _require_independent_contexts,
    _require_integration_list,
    _require_module_source,
    _require_owner,
    read_pilot,
)


@pytest.mark.parametrize('names', [None, {}, [], ['test.py', 'test.py'], [{}], [1]])
def test_integration_selection_cannot_be_absent_or_duplicated(names):
    with pytest.raises(ValueError):
        _require_integration_list(names)


@pytest.mark.parametrize(('module', 'source'), [
    ('../answer_budget', 'scripts/answer_budget.py'),
    ('answer_budget', 'scripts/answer_cost.py'),
    ('answer_budget', '../scripts/answer_budget.py'),
    ('', 'scripts/.py'),
])
def test_module_identity_cannot_point_elsewhere(module, source):
    with pytest.raises(ValueError):
        _require_module_source(module, source)


def test_nested_python_module_identity_is_supported():
    _require_module_source('llm_wiki.example', 'scripts/llm_wiki/example.py')


@pytest.mark.parametrize('declaration', [[], {}, {'schema': 'unknown'}])
def test_unknown_or_incomplete_schema_refuses(tmp_path, declaration):
    path = tmp_path / 'pilot.json'
    path.write_text(json.dumps(declaration), encoding='utf-8')
    with pytest.raises(ValueError):
        read_pilot(tmp_path, path)


def test_interface_must_belong_to_the_provider_owner():
    spec = {'provider_context': 'docs/modules/provider-context.json'}
    _require_owner({'api.pyi': 'provider'}, spec, 'provider', ['api.pyi'])
    with pytest.raises(ValueError, match='ownership'):
        _require_owner({'api.pyi': 'consumer'}, spec, 'provider', ['api.pyi'])
    with pytest.raises(ValueError, match='ownership'):
        _require_owner({}, spec, 'provider', ['api.pyi'])


def test_provider_and_consumer_cannot_share_ownership():
    with pytest.raises(ValueError, match='independent owners'):
        _require_independent_contexts({'provider_context': 'same.json',
                                      'consumer_context': 'same.json',
                                      'integration_context': 'integration.json'})
