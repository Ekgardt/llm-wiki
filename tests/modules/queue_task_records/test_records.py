"""Exact pre-transfer outputs captured from real queue lifecycle rows."""
import dataclasses
import json
from copy import deepcopy
from pathlib import Path

import pytest
from llm_wiki import queue_task_records as records
from llm_wiki.formats.schema import SchemaValidationError

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/existing-records.json').read_text())


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['label'])
def test_all_retained_fields_match_the_original_queue(case):
    row, history, payload = case['row'], case['history'], case['payload']
    task = records._task_record(row, history, payload)
    encoded = json.loads(json.dumps(dataclasses.asdict(task), default=lambda value: value.isoformat()))
    assert encoded == case['expected_task']
    assert task.payload is payload
    assert records._export_task_record(task) == case['expected_legacy']
    rows = records._ExportedRows(history, case['source_links'], case['link'], case['seal'])
    assert records._exported_task_record(row, payload, rows) == case['expected_v3']


def test_record_type_keeps_frozen_fields_and_original_lookup_identity():
    rows = records._ExportedRows([], [], None, None)
    assert type(rows).__module__ == 'memory_queue'
    with pytest.raises(dataclasses.FrozenInstanceError):
        rows.link = {}


@pytest.mark.parametrize('seal,expected', [(None, None), ({'seal_digest': 'c' * 64}, 'c' * 64)])
def test_capture_binding_retains_optional_seal_and_integer_version(seal, expected):
    link = {'intent_id': 'intent', 'intent_sha256': 'a' * 64,
            'handler_version': '3', 'link_digest': 'b' * 64}
    assert records._exported_capture_binding(link, seal) == {
        'intent_id': 'intent', 'intent_sha256': 'a' * 64, 'handler_version': 3,
        'active_digest': 'b' * 64, 'seal_digest': expected,
    }
    assert records._exported_capture_binding(None, seal) is None


def test_supplied_history_order_is_preserved_without_another_sort():
    case = FIXTURE['cases'][3]
    history = [{**case['history'][0], 'attempt': number} for number in (3, 1)]
    task = records._task_record(case['row'], history, case['payload'])
    assert [item.attempt for item in task.attempt_history] == [3, 1]
    assert [item['attempt'] for item in records._exported_attempt_history(history)] == [3, 1]


@pytest.mark.parametrize('field', ['lease_owner', 'lease_token', 'lease_expires_at'])
def test_partial_lease_is_not_silently_represented_as_absence(field):
    case = deepcopy(FIXTURE['cases'][1])
    case['row'][field] = None
    assert records._exported_lease(case['row']) is not None
    rows = records._ExportedRows(case['history'], case['source_links'], None, None)
    with pytest.raises(SchemaValidationError):
        records._exported_task_record(case['row'], case['payload'], rows)


def test_partial_result_is_rejected_by_the_same_schema():
    case = deepcopy(FIXTURE['cases'][3])
    case['row']['result_sha256'] = None
    assert records._exported_result(case['row']) is not None
    rows = records._ExportedRows(case['history'], case['source_links'], None, None)
    with pytest.raises(SchemaValidationError):
        records._exported_task_record(case['row'], case['payload'], rows)
