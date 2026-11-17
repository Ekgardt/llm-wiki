"""Pre-transfer field and error fixtures remain the contract."""
import annotationlib
import json
from dataclasses import MISSING, FrozenInstanceError, fields
from pathlib import Path
from typing import get_type_hints

import pytest
from llm_wiki import ownership_contracts as records

FIXTURE = json.loads(Path(__file__).with_name('original-contract.json').read_text(encoding='utf-8'))


def _field_contract(field, annotation):
    default = {'missing': True}
    if field.default is not MISSING:
        default = {'value': field.default}
    return {'name': field.name, 'type': annotation, 'default': default}


@pytest.mark.parametrize('name', FIXTURE['fields'])
def test_record_fields_defaults_and_frozen_binding(name):
    cls = getattr(records, name)
    annotations = annotationlib.get_annotations(cls, format=annotationlib.Format.STRING)
    assert [_field_contract(f, annotations[f.name]) for f in fields(cls)] == FIXTURE['fields'][name]
    resolved = get_type_hints(cls)
    assert all(f.type == resolved[f.name] for f in fields(cls))
    value = cls(**dict.fromkeys(f.name for f in fields(cls)))
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(cls)[0].name, 'changed')


@pytest.mark.parametrize('name', FIXTURE['errors'])
def test_error_arguments_message_and_state(name):
    expected = FIXTURE['errors'][name]
    value = getattr(records, name)(*expected['arguments'])
    assert str(value) == expected['message']
    assert json.loads(json.dumps(vars(value))) == expected['attributes']
