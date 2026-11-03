"""An interface cannot omit or silently change a provider declaration."""
import pytest

from tests.module_contract_surface import declared_kinds, declared_surface, require_declared_surface

PROVIDER = '''from __future__ import annotations
import json
BYTES_PER_TOKEN = 2
def estimate(data, *, scale: int = 2) -> int:
    return len(json.dumps(data)) // scale
'''
INTERFACE = '''from __future__ import annotations
BYTES_PER_TOKEN = 2
def estimate(data, *, scale: int = 2) -> int:
    ...
'''


def test_exact_surface_keeps_values_defaults_annotations_and_keyword_shape():
    assert require_declared_surface(PROVIDER, INTERFACE) == {'BYTES_PER_TOKEN', 'estimate'}


@pytest.mark.parametrize(('before', 'after'), [
    ('BYTES_PER_TOKEN = 2', 'BYTES_PER_TOKEN = 4'),
    ('scale: int = 2', 'scale: int = 3'),
    ('data, *,', 'data,'),
    ('-> int', '-> str'),
    ('def estimate', 'async def estimate'),
    ('BYTES_PER_TOKEN = 2\n', ''),
])
def test_changed_or_omitted_declaration_refuses(before, after):
    with pytest.raises(ValueError, match='requalification'):
        require_declared_surface(PROVIDER, INTERFACE.replace(before, after))


def test_new_export_requires_an_interface_update():
    with pytest.raises(ValueError, match='requalification'):
        require_declared_surface(PROVIDER + '\nADDED = True\n', INTERFACE)


def test_a_body_change_is_not_misrepresented_as_a_surface_change():
    changed = PROVIDER.replace('len(json.dumps(data)) // scale', '0')
    assert require_declared_surface(changed, INTERFACE) == {'BYTES_PER_TOKEN', 'estimate'}
    # The separate complete-byte identity must invalidate qualification on this edit.


@pytest.mark.parametrize('source', [
    'if enabled:\n    VALUE = 1',
    'class Public:\n    pass',
    'from dependency import *',
    'left = right = 1',
    'VALUE: int = 1',
    'VALUE = 1\nVALUE = 2',
    '@decorate\ndef public():\n    return 1',
    'globals()["VALUE"] = 1',
    'exec("VALUE = 1")',
])
def test_unsupported_or_ambiguous_public_declarations_refuse(source):
    with pytest.raises(ValueError):
        declared_surface(source)


def test_interface_cannot_be_executing_replacement_code():
    with pytest.raises(ValueError, match='ellipsis'):
        require_declared_surface(PROVIDER, INTERFACE.replace('...', 'return 0'))


def test_interface_cannot_import_hidden_implementation():
    with pytest.raises(ValueError, match='future imports'):
        require_declared_surface(PROVIDER, INTERFACE + '\nimport provider\n')


def test_private_implementation_is_not_a_published_interface():
    source = PROVIDER + '\n_PRIVATE = 1\ndef _helper():\n    return 1\n'
    assert require_declared_surface(source, INTERFACE) == {'BYTES_PER_TOKEN', 'estimate'}


def test_kinds_come_from_the_complete_checked_interface():
    interface = INTERFACE + '\ndef _private():\n    ...\n'
    assert declared_kinds(interface) == {'BYTES_PER_TOKEN': 'constant', 'estimate': 'function'}
