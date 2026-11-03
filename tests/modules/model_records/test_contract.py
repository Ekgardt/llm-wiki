"""The existing nominal records retain their values and representation."""
import inspect
import json
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import get_type_hints

import pytest
from context_budget import TokenUsage
from llm_wiki import model_records as records

NAMES = ('ProviderDescriptor', 'LLMResult', 'BackendResponse',
         '_CodexExecutable', 'CodexPlanningBasis')
ORIGINAL = json.loads((Path(__file__).parent / 'fixtures/original-records.json').read_text(encoding='utf-8'))


def _descriptor():
    return records.ProviderDescriptor('fake', None, {'structured_output': 'prompt'},
                                      {'max_tokens': 37}, 0, ())


@pytest.mark.parametrize('name', NAMES)
def test_original_fields_and_signatures_are_unchanged(name):
    kind = getattr(records, name)
    assert [field.name for field in fields(kind)] == ORIGINAL[name]['fields']
    assert str(inspect.signature(kind)) == ORIGINAL[name]['signature']
    assert kind.__module__ == 'llm_client'


@pytest.mark.parametrize('name', NAMES)
def test_runtime_annotations_resolve_without_a_client_import(name):
    kind = getattr(records, name)
    hints = get_type_hints(kind, vars(records))
    assert set(hints) == {field.name for field in fields(kind)}


def test_private_descriptor_state_is_not_canonical_or_comparable():
    public = _descriptor()
    private = replace(public, _endpoint='private endpoint', _resolution_failure='private failure')
    assert private == public
    assert repr(private) == repr(public)
    assert private.canonical() == {
        'provider': 'fake', 'model': None, 'capabilities': {'structured_output': 'prompt'},
        'inference_settings': {'max_tokens': 37}, 'candidate_index': 0, 'fallback_from': [],
    }
    assert private.identity == 'fake:<implicit>'
    assert private.resolution_failure == 'private failure'


def test_canonical_conversion_does_not_reinterpret_the_restricted_domain():
    descriptor = replace(_descriptor(), inference_settings={'unsupported': 0.5})
    with pytest.raises((TypeError, ValueError)):
        descriptor.canonical()


def test_records_keep_shallow_frozen_semantics():
    descriptor = _descriptor()
    with pytest.raises(FrozenInstanceError):
        descriptor.model = 'changed'
    descriptor.capabilities['fixture'] = True
    assert descriptor.capabilities['fixture'] is True


def test_usage_defaults_are_independent_and_distinguish_unknown_from_zero():
    first = records.BackendResponse('first')
    second = records.BackendResponse('second', TokenUsage(output_tokens=0))
    assert first.usage.output_tokens is None
    assert second.usage.output_tokens == 0
    assert records.BackendResponse('third').usage is not first.usage


def test_result_retains_the_five_positional_arguments():
    result = records.LLMResult(_descriptor(), 'answer', True, None, 'prompt')
    assert result.usage == TokenUsage()
    assert result.input_token_count is None
    assert result.failure_detail is None


def test_planning_descriptor_retains_original_values_and_the_basis_identity():
    original = _descriptor()
    executable = records._CodexExecutable('/fixture/bin', '/fixture/real', (1, 2), 'a' * 64)
    basis = records.CodexPlanningBasis(original, executable, 'resolved', 'fixture',
                                       None, 'b' * 64, (), 'c' * 64, 'fixture-version')
    descriptor = basis.descriptor
    assert descriptor == replace(original, model='resolved')
    assert descriptor._codex_basis is basis
    assert basis.original_descriptor is original
    assert original.model is None
    assert basis.mcp_server_names == ()
