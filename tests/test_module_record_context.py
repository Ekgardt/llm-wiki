"""Selected record bytes retain contained source paths and full catalog context."""
import json

import pytest

from tests.module_record_context import context_record_files, selected_entries
from tests.test_module_records import PARTS, SOURCE
from tests.test_module_records import catalog as catalog


@pytest.fixture
def workspace(tmp_path, catalog):
    source = tmp_path / catalog['source']
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE)
    name = 'docs/records.json'
    (tmp_path / name).write_text(json.dumps(catalog))
    manifest = {'records': [{'catalog': name, 'ids': ['second']}]}
    return tmp_path, manifest


def test_selected_entries_keep_exact_bytes_without_emitting_the_whole_source(workspace):
    root, manifest = workspace
    assert selected_entries(root, manifest) == {'@record/docs/history.md#second': PARTS[1]}
    assert (root / 'docs/history.md').read_bytes() == SOURCE


def test_catalog_and_complete_loader_contract_are_required(workspace):
    root, manifest = workspace
    assert context_record_files(root, manifest) == {root / name for name in [
        'docs/records.json', 'tests/module_records.py', 'tests/module_record_context.py',
        'tests/module_context_paths.py', 'docs/modules/context-records.md',
    ]}


def test_whole_file_packets_need_no_record_catalog(tmp_path):
    assert context_record_files(tmp_path, {}) == set()
    assert selected_entries(tmp_path, {}) == {}


def test_changed_unselected_source_bytes_fail(workspace):
    root, manifest = workspace
    (root / 'docs/history.md').write_bytes(SOURCE.replace(b'First', b'Other'))
    with pytest.raises(ValueError, match='digest changed'):
        selected_entries(root, manifest)


@pytest.mark.parametrize('name', ['../outside.json', '/outside.json', './docs/records.json', '.', 'docs\\records.json'])
def test_catalog_path_is_contained_and_canonical(workspace, name):
    root, manifest = workspace
    manifest['records'][0]['catalog'] = name
    with pytest.raises(ValueError):
        selected_entries(root, manifest)


def test_alias_source_does_not_claim_a_symlink_as_its_bytes(workspace, catalog):
    root, manifest = workspace
    link = root / 'docs/alias.md'
    try:
        link.symlink_to(root / 'docs/history.md')
    except OSError as exc:
        pytest.skip(f'symlink unavailable: {exc}')
    catalog['source'] = 'docs/alias.md'
    (root / 'docs/records.json').write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match='alias'):
        selected_entries(root, manifest)


def test_repeated_record_entries_cannot_overwrite_each_other(workspace):
    root, manifest = workspace
    manifest['records'] *= 2
    with pytest.raises(ValueError, match='Duplicate'):
        selected_entries(root, manifest)


@pytest.mark.parametrize('records', [None, {}, 'catalog', [{'catalog': 'docs/records.json'}]])
def test_malformed_record_declaration_is_not_ignored(tmp_path, records):
    with pytest.raises(ValueError):
        context_record_files(tmp_path, {'records': records})


def test_source_catalog_stays_the_declared_file(workspace):
    root, manifest = workspace
    name = manifest['records'][0]['catalog']
    before = (root / name).read_bytes()
    selected_entries(root, manifest)
    assert (root / name).read_bytes() == before
