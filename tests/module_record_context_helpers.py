"""Helpers shared by test_module_record_context.py and the test files importing them."""

import json

import pytest

from tests.module_records_helpers import SOURCE
from tests.module_records_helpers import catalog as catalog


@pytest.fixture
def workspace(tmp_path, catalog):
    source = tmp_path / catalog['source']
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE)
    name = 'docs/records.json'
    (tmp_path / name).write_text(json.dumps(catalog))
    manifest = {'records': [{'catalog': name, 'ids': ['second']}]}
    return tmp_path, manifest
