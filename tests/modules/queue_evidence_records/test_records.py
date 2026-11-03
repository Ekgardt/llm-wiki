"""Outputs captured from real pre-transfer capture, quarantine and purge tests."""
import dataclasses
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from llm_wiki import queue_evidence_records as records
from llm_wiki.formats.schema import SchemaValidationError
from llm_wiki.queue_contracts import QueueOperationError

CASES = json.loads((Path(__file__).parent / 'fixtures/existing-records.json').read_text(encoding='utf-8'))


def arguments(case):
    values = deepcopy(case['arguments'])
    if 'now' in values:
        values['now'] = datetime.fromisoformat(values['now'])
    for field in ('manifest_bytes', 'disposition_bytes'):
        if field in values:
            values[field] = values[field].encode()
    return values


@pytest.mark.parametrize('case', CASES.values(), ids=CASES.keys())
def test_actual_queue_outputs_remain_identical(case):
    result = getattr(records, case['name'])(**arguments(case))
    if isinstance(result, bytes):
        result = result.decode()
    assert result == case['expected']


@pytest.mark.parametrize('field', ['active_digest', 'consumer_kind', 'consumer_id', 'seal_digest'])
def test_seal_reuse_refuses_each_identity_change(field):
    values = arguments(CASES['_require_identical_seal'])
    values['existing'][field] = 'changed'
    with pytest.raises(QueueOperationError, match='capture_link_sealed'):
        records._require_identical_seal(**values)


@pytest.mark.parametrize('field', ['manifest_bytes', 'disposition_bytes'])
def test_package_byte_tampering_is_not_an_identical_record(field):
    values = arguments(CASES['_corrupt_records_match'])
    values[field] += b' '
    assert not records._corrupt_records_match(**values)


@pytest.mark.parametrize('record,field', [('manifest', 'task_id'), ('record', 'task_id'),
                                         ('manifest', 'package_path'), ('record', 'package_path')])
def test_package_names_must_also_match(record, field):
    values = arguments(CASES['_corrupt_records_match'])
    values[record][field] = 'different'
    assert not records._corrupt_records_match(**values)


@pytest.mark.parametrize('name', ['_corrupt_manifest', '_corrupt_disposition_record',
                                  '_capture_resolution_record-none', '_purge_receipt_record'])
def test_each_record_keeps_its_schema_refusal(name):
    case = CASES[name]
    values = arguments(case)
    values['task_id'] = ''
    with pytest.raises(SchemaValidationError):
        getattr(records, case['name'])(**values)


def test_selected_intent_and_observation_are_snapshots():
    values = arguments(CASES['_capture_resolution_record-none'])
    selected = {'intent_id': 'a' * 64, 'intent_sha256': 'b' * 64, 'handler_version': 1}
    values['selected_intent'] = selected
    result = records._capture_resolution_record(**values)
    assert result['selected_intent'] == selected
    assert result['selected_intent'] is not selected
    assert result['observed'] == values['observed']
    assert result['observed'] is not values['observed']


def test_evidence_value_retains_all_frozen_fields():
    value = records._CorruptEvidence(b'raw', b'history', b'metadata', 'a', 'b', 'c', 'key', 3)
    assert dataclasses.astuple(value) == (b'raw', b'history', b'metadata', 'a', 'b', 'c', 'key', 3)
    assert type(value).__module__ == 'memory_queue'
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.raw = b'changed'
