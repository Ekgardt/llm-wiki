"""The ownership and Markdown record classes and errors keep their old serialization, and the ownership validation aliases keep one serializable implementation."""

import base64
import json
import pickle

from tests.module_legacy_alias_helpers import (
    ROOT,
    _definitions,
)


def _record_fixture(module):
    path = ROOT / f'tests/modules/{module}/original-contract.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _assert_record_serialization(module, facade):
    import importlib

    records = importlib.import_module(f'llm_wiki.{module}')
    legacy = importlib.import_module(facade)
    fixture = _record_fixture(module)
    for name, encoded in fixture['classes'].items():
        value = getattr(records, name)
        assert getattr(legacy, name) is value
        assert pickle.loads(base64.b64decode(encoded)) is value
        assert pickle.dumps(value, protocol=4) == base64.b64decode(encoded)
    for name, expected in fixture['errors'].items():
        value = pickle.loads(base64.b64decode(expected['pickle']))
        assert type(value) is getattr(records, name)
        assert str(value) == expected['message']
        assert json.loads(json.dumps(vars(value))) == expected['attributes']
        assert pickle.dumps(value, protocol=4) == base64.b64decode(expected['pickle'])


def test_ownership_records_retain_old_serialization():
    _assert_record_serialization('ownership_contracts', 'operational_ownership')


def test_markdown_records_retain_old_serialization():
    _assert_record_serialization('markdown_records', 'markdown_transaction')


def test_ownership_validation_aliases_keep_one_serializable_implementation():
    import operational_ownership
    from llm_wiki import ownership_validation

    names = _definitions('scripts/llm_wiki/ownership_validation.py')
    assert not names & _definitions('scripts/operational_ownership.py')
    for name in names:
        value = getattr(ownership_validation, name)
        assert getattr(operational_ownership, name) is value
        assert pickle.loads(pickle.dumps(value, protocol=4)) is value
