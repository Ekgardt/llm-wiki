"""Byte identities and closed-schema semantics survive the module extraction."""
import hashlib

import pytest
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import SchemaValidationError, validate_schema_object
from llm_wiki.formats.source import SourceDescriptor


def test_unicode_normalization_preserves_known_canonical_bytes():
    expected = '{"a":[null,true,2],"é":"é"}'.encode()
    actual = canonical_json_bytes({"e\u0301": "e\u0301", "a": [None, True, 2]})
    assert actual == expected
    assert sha256_bytes(actual) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("value", [1.0, float('nan'), (1,), {1: 'x'}])
def test_noncanonical_values_are_rejected(value):
    with pytest.raises(TypeError):
        canonical_json_bytes(value)


def test_normalized_key_collision_is_not_silently_overwritten():
    with pytest.raises(ValueError, match="collision"):
        canonical_json_bytes({'é': 1, 'e\u0301': 2})


@pytest.mark.parametrize("value", [True, 1.5, float('inf'), '1'])
def test_integer_schema_does_not_accept_other_types(value):
    with pytest.raises(SchemaValidationError):
        validate_schema_object(value, {'type': 'integer'})


def test_internal_json_pointer_escapes_and_root_resolution():
    schema = {'$defs': {'a/b~c': {'type': 'integer'}}, '$ref': '#/$defs/a~1b~0c'}
    validate_schema_object(1, schema)
    with pytest.raises(SchemaValidationError):
        validate_schema_object('1', schema)


@pytest.mark.parametrize("path", ['../secret', '/absolute', 'C:/outside', 'a\\b', '.'])
def test_source_identity_rejects_paths_outside_logical_vault(path):
    with pytest.raises(ValueError):
        SourceDescriptor(path, 0, 'a' * 64).canonical()


def test_source_identity_keeps_path_and_size():
    descriptor = SourceDescriptor('knowledge/daily/a.md', 3, 'a' * 64)
    assert descriptor.canonical() == ['knowledge/daily/a.md', 3, 'a' * 64]
    assert descriptor.receipt_descriptor() == {
        'logical_path': 'knowledge/daily/a.md', 'byte_size': 3,
        'sha256': 'a' * 64, 'occurrence_bounds': None,
    }
