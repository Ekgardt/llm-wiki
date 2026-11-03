"""Moving owner contracts out of the global reference cannot drop needed guides."""
import json
import re
from pathlib import Path

import pytest

from tests.module_context import _contained, context_files
from tests.module_ownership import ownership_report
from tests.module_record_context import selected_entries

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / 'docs/modules').glob('*-context.json'))


def _context_owners(root, files, ownership, record_names):
    names = {p.relative_to(root).as_posix() for p in files} | set(record_names)
    return {ownership[name] for name in names if name in ownership}


def _required_guides(root, files, ownership, *, record_names=()):
    owners = _context_owners(root, files, ownership, record_names)
    pending = {root / 'docs/modules' / f'{owner}.md' for owner in owners}
    return _guide_closure(root, pending)


def _shared_targets(root, guide):
    lines = [line for line in guide.read_text(encoding='utf-8').splitlines() if line.startswith('Shared canonical contract:')]
    matches = [re.fullmatch(r'Shared canonical contract: \[.*?\]\(([^)]+)\)\.', line) for line in lines]
    assert all(matches), f'Malformed shared contract reference: {guide}'
    return {_contained(root, str(guide.parent.relative_to(root) / match.group(1))) for match in matches}


def _guide_closure(root, pending):
    seen = set()
    while pending:
        guide = pending.pop()
        if guide in seen:
            continue
        seen.add(guide)
        pending.update(_shared_targets(root, guide) - seen)
    return seen


@pytest.fixture(scope='module')
def ownership():
    report = ownership_report(ROOT)
    return {**report['ownership'], **report['record_ownership']}


@pytest.mark.parametrize('manifest', MANIFESTS, ids=lambda p: p.stem)
def test_packet_retains_full_guides_for_its_source_owners(manifest, ownership):
    data = json.loads(manifest.read_text(encoding='utf-8'))
    files = context_files(ROOT, data)
    needed = _required_guides(ROOT, files, ownership, record_names=selected_entries(ROOT, data))
    assert needed <= files, f'{manifest.name}: missing owner guides {sorted(needed - files)}'


def test_canonical_entry_point_links_each_owner_guide(ownership):
    source = (ROOT / 'docs/STRUCTURE.md').read_text(encoding='utf-8')
    for owner in set(ownership.values()):
        assert f'(modules/{owner}.md)' in source, owner


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


@pytest.mark.parametrize('manifest', MANIFESTS, ids=lambda p: p.stem)
def test_every_packet_retains_current_qualification_contract(manifest):
    files = context_files(ROOT, json.loads(manifest.read_text(encoding='utf-8')))
    assert ROOT / 'docs/modules/context-tooling.md' in files
