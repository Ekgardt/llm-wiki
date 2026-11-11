"""Runtime IO public names retain object identity and their serialized paths."""
import base64
import json
import pickle
from pathlib import Path

import pytest
import reliable_memory
from llm_wiki import runtime_contracts

FIXTURE = json.loads((Path(__file__).parent / 'modules/runtime_contracts/fixtures/legacy-pickle.json').read_text(encoding='utf-8'))


def _instance(name):
    instances = {
        'identity_instance': runtime_contracts.RuntimeFileIdentity('posix', '1', '2', 3, 4),
        'unsafe_instance': runtime_contracts.UnsafeStateRoot('example'),
        'durability_instance': runtime_contracts.MetadataDurabilityUnavailable(5, 'example'),
    }
    return instances[name]


@pytest.mark.parametrize('name', [name for name in FIXTURE if not name.endswith('_instance')])
def test_public_callable_pickle_is_byte_identical(name):
    value = getattr(reliable_memory, name)
    encoded = base64.b64decode(FIXTURE[name])
    assert pickle.dumps(value, protocol=4) == encoded
    assert pickle.loads(encoded) is value


@pytest.mark.parametrize('name', [name for name in FIXTURE if name.endswith('_instance')])
def test_legacy_record_and_exception_pickle_is_byte_identical(name):
    value = _instance(name)
    encoded = base64.b64decode(FIXTURE[name])
    assert pickle.dumps(value, protocol=4) == encoded
    assert type(pickle.loads(encoded)) is type(value)
