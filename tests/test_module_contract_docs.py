"""Moving owner contracts out of the global reference cannot drop needed guides."""

import pytest

from tests.module_contract_docs_helpers import _guide_closure, _required_guides


@pytest.mark.parametrize('name', [
    'scripts/unrelated_name.py', 'benchmark/tool.py', 'integrations/adapter.py',
    'tests/support.py', 'root_tool.py', 'benchmark/results.json',
    'docs/research/history.md', 'integrations/adapter.js',
])
def test_dependency_guide_uses_declared_owner_not_source_filename(tmp_path, name):
    source = tmp_path / name
    ownership = {name: 'actual-owner'}
    guide = tmp_path / 'docs/modules/actual-owner.md'
    guide.parent.mkdir(parents=True)
    guide.write_text('')
    assert _required_guides(tmp_path, {source}, ownership) == {
        tmp_path / 'docs/modules/actual-owner.md',
    }


def test_unowned_entries_do_not_invent_guides(tmp_path):
    assert _required_guides(tmp_path, {tmp_path / 'benchmark/unowned.py'}, {}) == set()


def test_selected_records_require_their_owner_not_only_the_catalog_owner(tmp_path):
    catalog = 'docs/modules/history-records.json'
    selected = '@record/docs/research/history.md#selected'
    unselected = '@record/docs/research/history.md#other'
    ownership = {catalog: 'catalog-owner', selected: 'selected-owner', unselected: 'other-owner'}
    guides = tmp_path / 'docs/modules'
    guides.mkdir(parents=True)
    (guides / 'catalog-owner.md').write_text('')
    (guides / 'selected-owner.md').write_text('')
    assert _required_guides(tmp_path, {tmp_path / catalog}, ownership, record_names={selected}) == {
        guides / 'catalog-owner.md', guides / 'selected-owner.md',
    }


def test_shared_contracts_are_included_recursively_with_cycles(tmp_path):
    first = tmp_path / 'docs/modules/first.md'
    second = tmp_path / 'docs/modules/second.md'
    first.parent.mkdir(parents=True)
    first.write_text('Shared canonical contract: [second](second.md).')
    second.write_text('Shared canonical contract: [first](first.md).')
    assert _guide_closure(tmp_path, {first}) == {first, second}


def test_malformed_shared_contract_is_not_silently_ignored(tmp_path):
    guide = tmp_path / 'guide.md'
    guide.write_text('Shared canonical contract: missing target')
    with pytest.raises(AssertionError, match='Malformed shared contract'):
        _guide_closure(tmp_path, {guide})


def test_shared_contract_cannot_escape_the_repository(tmp_path):
    guide = tmp_path / 'guide.md'
    guide.write_text('Shared canonical contract: [outside](../outside.md).')
    with pytest.raises(ValueError):
        _guide_closure(tmp_path, {guide})
