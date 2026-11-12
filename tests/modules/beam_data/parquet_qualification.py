"""Explicit optional suite: real Parquet is mandatory, with no absent-package skip."""
import json
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from benchmark import beam_data
from tests.beam_becomes_a_question_this_stand_can_read_helpers import CONVERSATION


def test_real_nested_unicode_parquet_round_trip_and_conversion(tmp_path):
    row = deepcopy(CONVERSATION)
    row['conversation_id'] = 'разговор-世界'
    row['chat'][0][0]['content'] = 'Привет 世界 🧭 ->-> 1,1'
    source = tmp_path / 'разговор.parquet'
    pq.write_table(pa.Table.from_pylist([row]), source)
    assert beam_data.load_rows(source) == [row]
    target = tmp_path / 'converted' / 'questions.json'
    assert beam_data.convert(source, target) == target
    questions = json.loads(target.read_text(encoding='utf-8'))
    assert questions == beam_data.converted_questions([row])
    assert len(questions) == 2
    assert questions[0]['haystack_sessions'][0][0]['content'] == 'Привет 世界 🧭'


def test_empty_real_parquet_emits_empty_questions(tmp_path):
    source = tmp_path / 'empty.parquet'
    pq.write_table(pa.table({'conversation_id': pa.array([], type=pa.string())}), source)
    assert beam_data.load_rows(source) == []
    target = tmp_path / 'empty.json'
    beam_data.convert(source, target)
    assert json.loads(target.read_text(encoding='utf-8')) == []


def test_invalid_parquet_refuses_without_overwriting_output(tmp_path):
    source = tmp_path / 'invalid.parquet'
    source.write_bytes(b'not a parquet file')
    target = tmp_path / 'questions.json'
    target.write_bytes(b'existing output')
    with pytest.raises(pa.ArrowInvalid):
        beam_data.convert(source, target)
    assert target.read_bytes() == b'existing output'


def test_missing_parquet_retains_the_named_dataset_refusal(tmp_path):
    with pytest.raises(beam_data.DatasetUnavailable, match='never generates'):
        beam_data.load_rows(tmp_path / 'missing.parquet')
