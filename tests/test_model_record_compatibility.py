"""Real legacy imports and pre-transfer synthetic pickle objects stay compatible."""
import base64
import json
import pickle
from pathlib import Path

import llm_client
import pytest
from llm_wiki import model_records

FIXTURE = Path(__file__).parent / 'modules/model_records/fixtures/original-records.json'
ORIGINAL = json.loads(FIXTURE.read_text())


@pytest.mark.parametrize('name', tuple(ORIGINAL))
def test_original_serialized_value_uses_the_same_public_class(name):
    kind = getattr(llm_client, name)
    assert kind is getattr(model_records, name)
    value = pickle.loads(base64.b64decode(ORIGINAL[name]['pickle_b64']))
    assert type(value) is kind
    assert repr(value) == ORIGINAL[name]['repr']
    assert pickle.loads(pickle.dumps(value, protocol=4)) == value
