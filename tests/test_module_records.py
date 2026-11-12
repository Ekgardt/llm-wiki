"""Record views preserve complete source coverage before selecting exact bytes."""
import copy
import json

import pytest

from tests import regenerate_module_records
from tests.module_records import read_catalog, record_entries, validated_records
from tests.module_records_helpers import (
    PARTS,
    SOURCE,
    _record,
    catalog,  # noqa: F401
)
from tests.regenerate_module_records import rebuilt


def test_complete_partition_reconstructs_original_bytes(catalog):  # noqa: F811
    parts = validated_records(catalog, SOURCE)
    assert parts == {'first': PARTS[0], 'second': PARTS[1]}
    assert b''.join(parts.values()) == SOURCE


def test_selection_keeps_original_bytes_and_source_identity(catalog):  # noqa: F811
    assert record_entries(catalog, SOURCE, ['second']) == {
        '@record/docs/history.md#second': PARTS[1],
    }
    assert record_entries(catalog, SOURCE, []) == {}


@pytest.mark.parametrize('source', [
    '../outside.md', '/docs/history.md', 'docs/../scripts/source.py',
    'docs\\history.md', './docs/history.md', 'scripts/source.py',
    'tests/test_source.py', 'knowledge/notes/private.md', 'docs/source.py', '', '.',
    'AGENTS.md', 'CLAUDE.md', 'README.md', 'rules/development-laws.md',
])
def test_record_views_cannot_hide_code_or_escape_artifact_classes(catalog, source):  # noqa: F811
    catalog['source'] = source
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


@pytest.mark.parametrize('source', ['docs/history.md', 'benchmark/cases.json', 'uv.lock', 'CHANGELOG.md'])
def test_approved_artifact_classes_are_explicit(catalog, source):  # noqa: F811
    catalog['source'] = source
    assert b''.join(validated_records(catalog, SOURCE).values()) == SOURCE


@pytest.mark.parametrize(('field', 'value'), [
    ('schema', 'unknown'), ('bytes', True), ('bytes', len(SOURCE) + 1),
    ('sha256', '0' * 64), ('records', []), ('records', {}),
])
def test_invalid_source_catalog_is_refused(catalog, field, value):  # noqa: F811
    catalog[field] = value
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


def test_changed_source_is_not_silently_reused(catalog):  # noqa: F811
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE.replace(b'First', b'Other'))


@pytest.mark.parametrize('action', ['missing', 'extra'])
def test_catalog_fields_cannot_drift(catalog, action):  # noqa: F811
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
def test_gaps_overlaps_invalid_ids_and_changed_records_fail(catalog, index, field, value):  # noqa: F811
    catalog['records'][index][field] = value
    with pytest.raises(ValueError):
        validated_records(catalog, SOURCE)


def test_full_hash_does_not_hide_a_split_inside_utf8(catalog):  # noqa: F811
    split = len(b'# First\n') + 1
    catalog['records'] = [_record('first', 0, SOURCE[:split]),
                          _record('second', split, SOURCE[split:])]
    with pytest.raises(UnicodeDecodeError):
        validated_records(catalog, SOURCE)


def test_selection_validates_unselected_records_too(catalog):  # noqa: F811
    catalog['records'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        record_entries(catalog, SOURCE, ['second'])


@pytest.mark.parametrize('selected', [['missing'], ['first', 'first'], [None], 'first'])
def test_unknown_duplicate_or_invalid_selection_fails(catalog, selected):  # noqa: F811
    with pytest.raises(ValueError):
        record_entries(catalog, SOURCE, selected)


def test_validation_and_selection_do_not_mutate_catalog(catalog):  # noqa: F811
    original = copy.deepcopy(catalog)
    record_entries(catalog, SOURCE, ['second', 'first'])
    assert catalog == original


def test_valid_prefix_cannot_claim_the_omitted_tail(catalog):  # noqa: F811
    catalog['records'].pop()
    with pytest.raises(ValueError, match='omits source tail'):
        validated_records(catalog, SOURCE)


def test_record_fields_cannot_silently_add_hidden_metadata(catalog):  # noqa: F811
    catalog['records'][0]['ignored'] = 'hidden'
    with pytest.raises(ValueError, match='fields changed'):
        validated_records(catalog, SOURCE)


def test_catalog_json_has_one_unambiguous_value_per_field(catalog):  # noqa: F811
    data = json.dumps(catalog).encode()
    assert read_catalog(data) == catalog
    duplicate = data.replace(b'"source":', b'"source": "docs/other.md", "source":', 1)
    with pytest.raises(ValueError, match='Duplicate'):
        read_catalog(duplicate)


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
def test_nonstandard_json_constants_are_rejected(constant):
    with pytest.raises(ValueError):
        read_catalog(('{"bytes": ' + constant + '}').encode())


@pytest.mark.parametrize(('edited', 'changed'), [
    (PARTS[0].replace(b'First', b'Much longer') + PARTS[1], 'first'),
    (PARTS[0] + PARTS[1].replace(b'Second', b'2nd'), 'second'),
    (PARTS[0] + b'inserted on the boundary\n' + PARTS[1], 'second'),
    (PARTS[0] + PARTS[1] + b'appended\n', 'second'),
], ids=['grows first', 'shrinks last', 'on the boundary', 'at the end'])
def test_regeneration_measures_the_one_edited_record_again(catalog, edited, changed):  # noqa: F811
    fresh = rebuilt(catalog, edited)
    assert b''.join(validated_records(fresh, edited).values()) == edited
    assert [row['id'] for row, old in zip(fresh['records'], catalog['records'], strict=True)
            if row['sha256'] != old['sha256']] == [changed]


def test_regeneration_refuses_an_edit_across_two_records(catalog):  # noqa: F811
    edited = PARTS[0].replace(b'First', b'1st') + PARTS[1].replace(b'Second', b'2nd')
    with pytest.raises(ValueError, match='first, second'):
        rebuilt(catalog, edited)


def test_regeneration_writes_a_catalog_the_validator_accepts(catalog, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(regenerate_module_records, 'ROOT', tmp_path)
    source = tmp_path / catalog['source']
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE + b'more\n')
    path = tmp_path / 'records.json'
    path.write_text(json.dumps(catalog, indent=2) + '\n', encoding='utf-8')
    assert regenerate_module_records.main([str(path)]) == 0
    assert path.read_text(encoding='utf-8').endswith('\n')
    assert b''.join(validated_records(json.loads(path.read_text(encoding='utf-8')), source.read_bytes()).values()) == source.read_bytes()
