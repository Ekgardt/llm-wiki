"""Correct links fit the bounded plan regardless of their separate count."""
import json

import compile_memory
import jsonschema
import pytest
from markdown_transaction import MarkdownCoordinator
from reliable_memory import canonical_json_bytes

from tests.test_compile_transactions import _daily, _semantic_plan
from tests.test_compile_transactions import vault as vault


def test_a_small_plan_with_65_links_is_committed_without_loss(vault):
    root, state_root = vault
    inputs = compile_memory.snapshot_compile_inputs([_daily(root)])
    plan = _semantic_plan()
    operation = json.loads(plan['operations'][0]['content'])
    links = [f'[[Related concept {number}]]' for number in range(65)]
    operation['related'] = links
    plan['operations'][0]['content'] = canonical_json_bytes(operation).decode()
    compile_memory._require_bounded_response(json.dumps({'operations': [operation]}))
    result = compile_memory.apply_compile_plan(
        inputs, plan, action_key='a' * 64, trigger='manual',
        coordinator=MarkdownCoordinator(root, state_root),
    )
    content = (root / 'knowledge/notes/exact-byte-pattern.md').read_text()
    assert (result.state, all(link in content for link in links)) == ('committed', True)


def test_the_provider_schema_admits_the_same_65_correct_links():
    schema = compile_memory.RAW_PLAN_SCHEMA['properties']['operations']['items']['properties']['related']
    links = [f'[[Related concept {number}]]' for number in range(65)]
    jsonschema.validate(links, schema)


@pytest.mark.parametrize('links', ['[[A]]', [None], ['A'], ['[[A\nB]]']])
def test_invalid_links_are_still_refused(links):
    with pytest.raises(ValueError, match='related links'):
        compile_memory._require_semantic_links({'related': links})


def test_the_whole_provider_response_budget_still_refuses_oversize():
    with pytest.raises(ValueError, match='provider response exceeds byte limit'):
        compile_memory._require_bounded_response('x' * (compile_memory.MAX_PROVIDER_RESPONSE_BYTES + 1))
