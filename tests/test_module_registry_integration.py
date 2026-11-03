"""Whole-checkout registry assertions; separate from tooling unit packets."""
import json
from pathlib import Path

import pytest

from tests.module_context import context_files
from tests.module_ownership import ownership_report
from tests.module_record_context import selected_entries
from tests.test_module_context_reads import _unchecked_reads
from tests.test_module_contract_docs import _required_guides

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / 'docs/modules').glob('*-context.json'))


@pytest.mark.parametrize('path', MANIFESTS, ids=lambda p: p.stem)
def test_existing_packets_include_actual_pytest_bootstrap(path):
    manifest = json.loads(path.read_text(encoding='utf-8'))
    assert {'tests/conftest.py', 'tests/code_kernel_helpers.py'} <= set(manifest['files'])


def test_actual_declared_packets_have_complete_local_source_closure():
    from tests.module_context import context_files, required_source_context
    root = Path(__file__).resolve().parents[1]
    for manifest in (root / 'docs/modules').glob('*-context.json'):
        files = context_files(root, json.loads(manifest.read_text(encoding='utf-8')))
        assert required_source_context(root, files) <= files, manifest.name


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


@pytest.mark.parametrize('manifest', MANIFESTS, ids=lambda p: p.stem)
def test_every_packet_retains_current_qualification_contract(manifest):
    files = context_files(ROOT, json.loads(manifest.read_text(encoding='utf-8')))
    assert ROOT / 'docs/modules/context-tooling.md' in files


def test_real_registry_does_not_confuse_packets_with_project_completion():
    root = Path(__file__).resolve().parents[1]
    report = ownership_report(root)
    assert report['owned_count'] > 0
    assert report['owned_count'] + len(report['unassigned']) == report['tracked_count']
    assert set(report['ownership']).isdisjoint(report['unassigned'])
    assert report['complete'] is (not report['unassigned'])


@pytest.mark.parametrize('path', sorted((ROOT / 'tests/modules').glob('*/test_boundary.py')) + [ROOT / 'tests/module_boundaries.py'],
                         ids=lambda p: p.parent.name)
def test_module_boundary_sources_use_checked_reads(path):
    assert not _unchecked_reads(path.read_text(encoding='utf-8')), path.relative_to(ROOT)


TOOLING_UNITS = (
    'test_module_context_bootstrap.py', 'test_module_context_dependencies.py',
    'test_module_contract_docs.py', 'test_module_ownership.py',
    'test_module_context_reads.py',
)


def _has_name(node, names):
    import ast
    return any(isinstance(child, ast.Name) and child.id in names for child in ast.walk(node))


def _checkout_root_assignment(node):
    import ast
    if not isinstance(node, ast.Assign):
        return set()
    if not _has_name(node.value, {'__file__'}):
        return set()
    return {target.id for target in node.targets if isinstance(target, ast.Name)}


def _is_checkout_scan(node, roots):
    import ast
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in {'glob', 'rglob'} and _has_name(node.func.value, roots)


def _checkout_scans(source):
    import ast
    tree = ast.parse(source)
    roots = set().union(*(_checkout_root_assignment(node) for node in ast.walk(tree)))
    return [node.lineno for node in ast.walk(tree) if _is_checkout_scan(node, roots)]


@pytest.mark.parametrize('source', [
    "ROOT = Path(__file__).resolve().parents[1]\n(ROOT / 'docs').glob('*.json')",
    "def check():\n root = Path(__file__).parents[1]\n return root.rglob('*.py')",
])
def test_registry_scan_guard_detects_checkout_discovery(source):
    assert _checkout_scans(source)


def test_registry_scan_guard_allows_synthetic_fixture_discovery():
    assert not _checkout_scans("def check(tmp_path):\n return tmp_path.glob('*.json')")


@pytest.mark.parametrize('name', TOOLING_UNITS)
def test_tooling_units_do_not_discover_the_checkout_registry(name):
    assert not _checkout_scans((ROOT / 'tests' / name).read_text(encoding='utf-8')), name
