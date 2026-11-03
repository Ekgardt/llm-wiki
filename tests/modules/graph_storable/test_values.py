"""Storage adapters preserve their existing finite, caller-qualified policy."""
from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest
from graph_storable import storable_identity_key, storable_metadata, valid_graph_text


@pytest.mark.parametrize(('value', 'maximum', 'expected'), [
    ('a', 1, True), ('ab', 1, False), ('', 1, False), (None, 3, False),
    ('a\x00b', 3, False), ('a\nb', 3, False), ('a\rb', 3, False),
    ('a\tb', 3, True), ('文', 1, True), ('a', 0, False),
])
def test_text_admission_edges(value, maximum, expected):
    assert valid_graph_text(value, maximum) is expected


def test_identity_digest_has_independent_known_value():
    assert storable_identity_key('') == 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    assert storable_identity_key('ordinary') == 'ordinary'


def test_identity_threshold_does_not_truncate_distinct_suffixes():
    prefix = 'k' * 4096
    assert storable_identity_key(prefix) == prefix
    left = storable_identity_key(prefix + 'a')
    right = storable_identity_key(prefix + 'b')
    assert left.startswith('sha256:')
    assert len(left) == 71
    assert left != right


@pytest.mark.parametrize(('value', 'expected'), [
    (1.5, '1.5'), (-0.0, '-0.0'), (float('nan'), None),
    (float('inf'), None), (-float('inf'), None),
    (date(2026, 11, 3), '2026-11-03'),
    (datetime(2026, 11, 3, tzinfo=timezone.utc), '2026-11-03T00:00:00+00:00'),
    ('a\ud800b', 'a?b'), (True, True), (42, 42), (None, None),
])
def test_scalar_conversion_contract(value, expected):
    assert storable_metadata(value) == expected


def test_nested_mapping_and_tuple_are_copied():
    source = MappingProxyType({3: ('a\ud800', [1.25, date(2026, 11, 3)])})
    assert storable_metadata(source) == {'3': ['a?', ['1.25', '2026-11-03']]}
    assert source[3][0] == 'a\ud800'


def test_unsupported_objects_are_not_claimed_as_valid_json():
    value = object()
    assert storable_metadata(value) is value


def test_invalid_key_digest_preserves_surrogate_encoding():
    # UTF-8 surrogatepass bytes ED A0 80 followed by LF, independently hashed.
    assert storable_identity_key('\ud800\n') == 'sha256:31f87665300f4037ae1a1175e9c1c307c9fd09ced211af15b56cc6376e453762'
