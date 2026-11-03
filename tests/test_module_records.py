"""Record views preserve complete source coverage before selecting exact bytes."""
import copy
import hashlib
import json

import pytest

from tests.module_records import read_catalog, record_entries, validated_records

PARTS = [b'# First\n' + 'Я\n'.encode(), b'# Second\n' + '文\n'.encode()]
SOURCE = b''.join(PARTS)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _record(identifier, start, content):
    return {'id': identifier, 'label': identifier, 'start': start,
            'end': start + len(content), 'sha256': _digest(content)}


@pytest.fixture
def catalog():
    return {'schema': 'module-records/v1', 'source': 'docs/history.md',
            'bytes': len(SOURCE), 'sha256': _digest(SOURCE),
            'records': [_record('first', 0, PARTS[0]),
                        _record('second', len(PARTS[0]), PARTS[1])]}


def test_complete_partition_reconstructs_original_bytes(catalog):
    parts = validated_records(catalog, SOURCE)
    assert parts == {'first': PARTS[0], 'second': PARTS[1]}
    assert b''.join(parts.values()) == SOURCE


def test_selection_keeps_original_bytes_and_source_identity(catalog):
    assert record_entries(catalog, SOURCE, ['second']) == {
        '@record/docs/history.md#second': PARTS[1],
    }
    assert record_entries(catalog, SOURCE, []) == {}


@pytest.mark.parametrize('source', [
    '../outside.md', '/docs/history.md', 'docs/../scripts/source.py',
    'docs\\history.md', './docs/history.md', 'scripts/source.py',
    'tests/test_source.py', 'knowledge/notes/private.md', 'docs/source.py', '', '.',
])
def test_record_views_cannot_hide_code_or_escape_artifact_classes(catalog, source):
    catalog['source'] = source
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


@pytest.mark.parametrize('source', ['docs/history.md', 'benchmark/cases.json', 'uv.lock'])
def test_approved_artifact_classes_are_explicit(catalog, source):
    catalog['source'] = source
    assert b''.join(validated_records(catalog, SOURCE).values()) == SOURCE


@pytest.mark.parametrize(('field', 'value'), [
    ('schema', 'unknown'), ('bytes', True), ('bytes', len(SOURCE) + 1),
    ('sha256', '0' * 64), ('records', []), ('records', {}),
])
def test_invalid_source_catalog_is_refused(catalog, field, value):
    catalog[field] = value
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


def test_changed_source_is_not_silently_reused(catalog):
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE.replace(b'First', b'Other'))


@pytest.mark.parametrize('action', ['missing', 'extra'])
def test_catalog_fields_cannot_drift(catalog, action):
    if action == 'missing':
        del catalog['sha256']
    else:
        catalog['ignored'] = 'hidden'
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


@pytest.mark.parametrize(('index', 'field', 'value'), [
    (0, 'start', 1), (0, 'start', False), (0, 'end', 0),
    (0, 'end', len(SOURCE) + 1), (1, 'start', len(PARTS[0]) - 1),
    (1, 'start', len(PARTS[0]) + 1), (1, 'end', len(SOURCE) - 1),
    (1, 'id', 'first'), (1, 'id', ''), (1, 'id', '../second'),
    (1, 'label', ''), (1, 'sha256', '0' * 64),
])
def test_gaps_overlaps_invalid_ids_and_changed_records_fail(catalog, index, field, value):
    catalog['records'][index][field] = value
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


def test_full_hash_does_not_hide_a_split_inside_utf8(catalog):
    split = len(b'# First\n') + 1
    catalog['records'] = [_record('first', 0, SOURCE[:split]),
                          _record('second', split, SOURCE[split:])]
    with pytest.raises(UnicodeDecodeError):
        validated_records(catalog, SOURCE)


def test_selection_validates_unselected_records_too(catalog):
    catalog['records'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        record_entries(catalog, SOURCE, ['second'])


@pytest.mark.parametrize('selected', [['missing'], ['first', 'first'], [None], 'first'])
def test_unknown_duplicate_or_invalid_selection_fails(catalog, selected):
    with pytest.raises(ValueError):
        record_entries(catalog, SOURCE, selected)


def test_validation_and_selection_do_not_mutate_catalog(catalog):
    original = copy.deepcopy(catalog)
    record_entries(catalog, SOURCE, ['second', 'first'])
    assert catalog == original


def test_valid_prefix_cannot_claim_the_omitted_tail(catalog):
    catalog['records'].pop()
    with pytest.raises(ValueError, match='omits source tail'):
        validated_records(catalog, SOURCE)


def test_record_fields_cannot_silently_add_hidden_metadata(catalog):
    catalog['records'][0]['ignored'] = 'hidden'
    with pytest.raises(ValueError, match='fields changed'):
        validated_records(catalog, SOURCE)


def test_catalog_json_has_one_unambiguous_value_per_field(catalog):
    data = json.dumps(catalog).encode()
    assert read_catalog(data) == catalog
    duplicate = data.replace(b'"source":', b'"source": "docs/other.md", "source":', 1)
    with pytest.raises(ValueError, match='Duplicate'):
        read_catalog(duplicate)


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
def test_nonstandard_json_constants_are_rejected(constant):
    with pytest.raises(ValueError):
        read_catalog(('{"bytes": ' + constant + '}').encode())
