"""Moving owner contracts out of the global reference cannot drop needed guides."""
import json
import re
from pathlib import Path

import pytest

from tests.module_context import _contained, context_files
from tests.module_ownership import ownership_report

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / 'docs/modules').glob('*-context.json'))


def _source_owners(root, files, ownership):
    names = {p.relative_to(root).as_posix() for p in files if p.is_relative_to(root / 'scripts')}
    return {ownership[name] for name in names if name in ownership}


def _required_guides(root, files, ownership):
    pending = {root / 'docs/modules' / f'{owner}.md' for owner in _source_owners(root, files, ownership)}
    return _guide_closure(root, pending)


def _shared_targets(root, guide):
    lines = [line for line in guide.read_text().splitlines() if line.startswith('Shared canonical contract:')]
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
    return ownership_report(ROOT)['ownership']


@pytest.mark.parametrize('manifest', MANIFESTS, ids=lambda p: p.stem)
def test_packet_retains_full_guides_for_its_source_owners(manifest, ownership):
    files = context_files(ROOT, json.loads(manifest.read_text()))
    needed = _required_guides(ROOT, files, ownership)
    assert needed <= files, f'{manifest.name}: missing owner guides {sorted(needed - files)}'


def test_canonical_entry_point_links_each_owner_guide(ownership):
    source = (ROOT / 'docs/STRUCTURE.md').read_text()
    for owner in set(ownership.values()):
        assert f'(modules/{owner}.md)' in source, owner


def test_dependency_guide_uses_declared_owner_not_source_filename(tmp_path):
    source = tmp_path / 'scripts/unrelated_name.py'
    ownership = {'scripts/unrelated_name.py': 'actual-owner'}
    guide = tmp_path / 'docs/modules/actual-owner.md'
    guide.parent.mkdir(parents=True)
    guide.write_text('')
    assert _required_guides(tmp_path, {source}, ownership) == {
        tmp_path / 'docs/modules/actual-owner.md',
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
