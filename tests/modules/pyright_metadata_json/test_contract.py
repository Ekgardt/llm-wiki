"""Exact before-transfer outcomes for the existing metadata dialect."""
import base64
import json
from pathlib import Path

import pytest
from llm_wiki import pyright_metadata_json as codec

BASELINE = json.loads(Path(__file__).with_name('baseline.json').read_text())


def _normalize(case, raw):
    if case['jsonc']:
        return codec._normalize_jsonc(raw, 'bad')
    return raw


def _outcome(case):
    try:
        normalized = _normalize(case, base64.b64decode(case['raw']))
        value = codec._strict_json_object(normalized, 'bad', recursion_code='deep')
        return {'normalized': base64.b64encode(normalized).decode(), 'value': value}
    except codec._MetadataError as error:
        return {'code': error.code}


@pytest.mark.parametrize('case', BASELINE['cases'], ids=[c['name'] for c in BASELINE['cases']])
def test_original_bytes_values_and_failure_codes(case):
    assert _outcome(case) == case['expected']


def _recursion_error(*_args, **_kwargs):
    raise RecursionError('decoder recursion failure')


@pytest.mark.parametrize('recursion,expected', [(None, 'bad'), ('deep', 'deep')])
def test_decoder_recursion_error_retains_callers_selected_code(monkeypatch, recursion, expected):
    with monkeypatch.context() as patch:
        patch.setattr(codec.json, 'loads', _recursion_error)
        with pytest.raises(codec._MetadataError) as raised:
            codec._strict_json_object(b'{}', 'bad', recursion_code=recursion)
    assert raised.value.code == expected
    assert isinstance(raised.value.__cause__, RecursionError)


def test_normalization_preserves_newlines_inside_comments():
    raw = b'{\r\n/*a\nb*/ "x":1}'
    assert codec._normalize_jsonc(raw, 'bad') == b'{\r\n   \n    "x":1}'


def test_metadata_exception_keeps_original_module_identity():
    assert codec._MetadataError.__module__ == BASELINE['exception_module']
