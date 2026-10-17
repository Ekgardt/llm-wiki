"""Receipt records are a set of bindings; semantic assertions remain ordered."""
import json

import compile_memory as cm
import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema_object

from tests.test_compile_transactions import _daily, _semantic_plan
from tests.test_compile_transactions import vault as vault


def _duplicate_plan():
    plan = _semantic_plan()
    operation = plan['operations'][0]
    semantic = json.loads(operation['content'])
    second = dict(semantic['evidence'][0], claim='The same source also supports a different assertion.')
    semantic['evidence'].append(second)
    operation['content'] = canonical_json_bytes(semantic).decode()
    return plan


def test_same_quote_distinct_claims_keep_both_assertions_and_one_receipt_binding(vault):
    root, _state = vault
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    plan = _duplicate_plan()
    operations, evidence = cm._materialized_operations(plan['operations'], inputs, '2026-07-14T11:00:00Z')
    assert len(operations) == 1
    assert len(evidence) == 1
    semantic, bindings = cm._validate_semantic_operation(json.loads(plan['operations'][0]['content']), inputs)
    assert len(semantic['evidence']) == len(bindings) == 2
    page = cm._render_page(semantic, '2026-07-14T11:00:00Z', [item['reference'] for item in bindings])
    assert all(item['claim'].encode() in page for item in semantic['evidence'])


def test_duplicate_legacy_receipt_binding_is_a_single_exact_record():
    binding = dict(reference='unused', source_path='knowledge/daily/2026-07-14.md', source_digest='a'*64, quote_sha256='b'*64)
    records = cm._bound_evidence('knowledge/notes/one.md', [binding, dict(binding)])
    assert len(records) == 1


@pytest.mark.parametrize('field,value', [('source_path', 'knowledge/daily/2026-07-15.md'), ('source_digest', 'c'*64), ('quote_sha256', 'd'*64)])
def test_receipt_dedup_keeps_every_distinct_full_record(field, value):
    first = dict(reference='unused', source_path='knowledge/daily/2026-07-14.md', source_digest='a'*64, quote_sha256='b'*64)
    second = dict(first, **{field: value})
    assert len(cm._bound_evidence('knowledge/notes/one.md', [first, second])) == 2


def test_dedup_never_merges_distinct_operations():
    binding = dict(reference='unused', source_path='knowledge/daily/2026-07-14.md', source_digest='a'*64, quote_sha256='b'*64)
    first = cm._bound_evidence('knowledge/notes/one.md', [binding])
    second = cm._bound_evidence('knowledge/notes/two.md', [binding])
    assert first != second


def test_duplicate_binding_does_not_hide_source_tampering(vault):
    root, _state = vault
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    semantic = json.loads(_duplicate_plan()['operations'][0]['content'])
    bindings = cm._validate_semantic_operation(semantic, inputs)[1]
    changed = dict(bindings[0], reference=bindings[0]['reference'].replace(inputs.dailies[0].sha256, 'f'*64))
    with pytest.raises(ValueError):
        cm._bound_evidence('knowledge/notes/one.md', [bindings[0], changed], inputs)


def test_unique_items_schema_still_refuses_duplicate_records():
    schema = {'type': 'array', 'uniqueItems': True}
    record = dict(operation_path='knowledge/notes/one.md', source_path='knowledge/daily/2026-07-14.md', source_digest=sha256_bytes(b'source'), quote_sha256=sha256_bytes(b'quote'))
    with pytest.raises(ValueError, match='uniqueItems'):
        validate_schema_object([record, dict(record)], schema)


def test_real_v4_publication_preserves_both_claims(vault):
    root, state = vault
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    batch = cm.pack_compile_batches(inputs, model=None)[0]
    plan = _duplicate_plan()
    from markdown_transaction import MarkdownCoordinator
    paths = cm.apply_compile_plan(
        inputs, plan, action_key='a'*64, trigger='manual',
        coordinator=MarkdownCoordinator(root, state), completed_at='2026-07-14T12:00:00Z',
        batch=batch, provider_budget={'provider': 'fake', 'model': 'fake-v1', 'max_output_tokens': 4000},
    )
    assert paths
    page = (root / plan['operations'][0]['path']).read_bytes()
    semantic = json.loads(plan['operations'][0]['content'])
    assert all(item['claim'].encode() in page for item in semantic['evidence'])
    descriptor = cm._v4_source_descriptor(inputs.dailies[0])
    identity = cm.compile_context_source_identity(descriptor)
    receipt = (root / f'knowledge/daily/receipts/v4-{identity}.md').read_bytes()
    assert receipt


def test_legacy_extra_fields_are_part_of_exact_record_identity():
    first = dict(reference='unused', source_path='knowledge/daily/2026-07-14.md', source_digest='a'*64, quote_sha256='b'*64, retained_field='one')
    second = dict(first, retained_field='two')
    records = cm._bound_evidence('knowledge/notes/one.md', [first, second, dict(first)])
    assert len(records) == 2
    assert [item['retained_field'] for item in records] == ['one', 'two']
