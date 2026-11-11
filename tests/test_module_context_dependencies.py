"""Local source imports cannot disappear behind a small declared packet."""
import json

import pytest

from tests.module_context import measure_context


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _packet(root, sources, included):
    required = {
        'tests/module_context.py': '', 'scripts/llm_wiki/context_bundle.py': '',
        'docs/modules/context-tooling.md': 'Full current qualification contract',
        'AGENTS.md': '', 'CLAUDE.md': '', 'rules/development-laws.md': '',
        'docs/AGENTS.md': '', 'docs/STRUCTURE.md': '',
    }
    for name, body in (required | sources).items():
        _write(root, name, body)
    return _write(root, 'context.json', json.dumps({
        'encoding': 'o200k_base', 'max_tokens': 100000,
        'serialization': 'exact-duplicates/v1',
        'files': [*required, *included, 'context.json'],
    }))


@pytest.mark.parametrize('source', [
    'import dependency', 'import dependency as alias', 'from dependency import VALUE',
    'def read():\n import dependency', 'if False:\n import dependency',
])
def test_omitted_absolute_dependency_is_named_before_counting(tmp_path, source):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': source, 'scripts/dependency.py': 'VALUE = 1',
    }, ['scripts/owner.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_transitive_dependency_is_required(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': 'import middle', 'scripts/middle.py': 'import dependency',
        'scripts/dependency.py': '',
    }, ['scripts/owner.py', 'scripts/middle.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('owner', ['benchmark/owner.py', 'integrations/owner.py', 'owner.py'])
@pytest.mark.parametrize('statement', ['import dependency', 'def later():\n import dependency'])
def test_every_declared_python_source_requires_its_product_dependencies(tmp_path, owner, statement):
    manifest = _packet(tmp_path, {
        owner: statement, 'scripts/dependency.py': 'VALUE = 1',
    }, [owner])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('dependency', ['benchmark/helper.py', 'helper.py', 'tests/helper.py'])
def test_launch_root_imports_cannot_be_mistaken_for_installed_packages(tmp_path, dependency):
    manifest = _packet(tmp_path, {
        'tests/check.py': 'import helper', dependency: 'VALUE = 1',
    }, ['tests/check.py'])
    with pytest.raises(AssertionError, match=dependency):
        measure_context(tmp_path, manifest)


def test_a_benchmark_sibling_import_is_followed(tmp_path):
    manifest = _packet(tmp_path, {
        'benchmark/nested/owner.py': 'import sibling',
        'benchmark/nested/sibling.py': 'VALUE = 1',
    }, ['benchmark/nested/owner.py'])
    with pytest.raises(AssertionError, match='benchmark/nested/sibling.py'):
        measure_context(tmp_path, manifest)


def test_all_ambiguous_local_candidates_are_required(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': 'import helper', 'scripts/helper.py': '',
        'benchmark/helper.py': '',
    }, ['scripts/owner.py', 'scripts/helper.py'])
    with pytest.raises(AssertionError, match='benchmark/helper.py'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('statement', ['from . import child', 'from .child import VALUE'])
def test_benchmark_relative_children_are_required(tmp_path, statement):
    manifest = _packet(tmp_path, {
        'benchmark/pkg/__init__.py': '', 'benchmark/pkg/owner.py': statement,
        'benchmark/pkg/child.py': 'VALUE = 1',
    }, ['benchmark/pkg/__init__.py', 'benchmark/pkg/owner.py'])
    with pytest.raises(AssertionError, match='benchmark/pkg/child.py'):
        measure_context(tmp_path, manifest)


def test_benchmark_initializers_and_their_imports_are_followed(tmp_path):
    manifest = _packet(tmp_path, {
        'benchmark/pkg/__init__.py': 'import dependency',
        'benchmark/pkg/owner.py': '', 'scripts/dependency.py': '',
    }, ['benchmark/pkg/owner.py'])
    with pytest.raises(AssertionError, match='benchmark/pkg/__init__.py'):
        measure_context(tmp_path, manifest)
    manifest.write_text(manifest.read_text(encoding='utf-8').replace(
        '"benchmark/pkg/owner.py"', '"benchmark/pkg/owner.py", "benchmark/pkg/__init__.py"'
    ))
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_relative_benchmark_import_cannot_escape_the_repository(tmp_path):
    manifest = _packet(tmp_path, {
        'benchmark/pkg/owner.py': 'from .... import outside',
    }, ['benchmark/pkg/owner.py'])
    with pytest.raises(AssertionError, match='Relative import escapes'):
        measure_context(tmp_path, manifest)


def test_complete_benchmark_cycle_and_installed_dependency_are_accepted(tmp_path):
    sources = {
        'benchmark/owner.py': 'import helper\nimport numpy',
        'benchmark/helper.py': 'import owner\nimport product',
        'scripts/product.py': '',
    }
    manifest = _packet(tmp_path, sources, list(sources))
    assert measure_context(tmp_path, manifest)[0] > 0


@pytest.mark.parametrize('source', ['from .dependency import VALUE', 'from . import dependency'])
def test_relative_dependency_is_required(tmp_path, source):
    manifest = _packet(tmp_path, {
        'scripts/pkg/__init__.py': '', 'scripts/pkg/owner.py': source,
        'scripts/pkg/dependency.py': 'VALUE = 1',
    }, ['scripts/pkg/__init__.py', 'scripts/pkg/owner.py'])
    with pytest.raises(AssertionError, match='scripts/pkg/dependency.py'):
        measure_context(tmp_path, manifest)


def test_package_initializer_is_required(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': 'import pkg.child', 'scripts/pkg/__init__.py': '',
        'scripts/pkg/child.py': '',
    }, ['scripts/owner.py', 'scripts/pkg/child.py'])
    with pytest.raises(AssertionError, match='scripts/pkg/__init__.py'):
        measure_context(tmp_path, manifest)


def test_imported_submodule_is_required(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': 'from pkg import child', 'scripts/pkg/__init__.py': '',
        'scripts/pkg/child.py': '',
    }, ['scripts/owner.py', 'scripts/pkg/__init__.py'])
    with pytest.raises(AssertionError, match='scripts/pkg/child.py'):
        measure_context(tmp_path, manifest)


def test_initializer_imports_are_followed(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/owner.py': 'import pkg.child', 'scripts/pkg/__init__.py': 'import dependency',
        'scripts/pkg/child.py': '', 'scripts/dependency.py': '',
    }, ['scripts/owner.py', 'scripts/pkg/__init__.py', 'scripts/pkg/child.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_complete_cycles_namespace_and_installed_imports_are_accepted(tmp_path):
    sources = {
        'scripts/first.py': 'import second\nimport ns.child\nimport json\nimport tiktoken',
        'scripts/second.py': 'import first', 'scripts/ns/child.py': '',
    }
    manifest = _packet(tmp_path, sources, list(sources))
    total, files = measure_context(tmp_path, manifest)
    assert total > 0
    assert files == 12


def test_local_dependency_symlink_outside_root_is_refused(tmp_path):
    root = tmp_path / 'repo'
    outside = _write(tmp_path, 'outside.py', '')
    manifest = _packet(root, {'scripts/owner.py': 'import dependency'}, ['scripts/owner.py'])
    try:
        (root / 'scripts/dependency.py').symlink_to(outside)
    except OSError as exc:
        pytest.skip(f'symlink unavailable: {exc}')
    with pytest.raises(ValueError):
        measure_context(root, manifest)


def test_declared_source_own_package_initializer_is_required(tmp_path):
    manifest = _packet(tmp_path, {
        'scripts/pkg/__init__.py': 'VALUE = 1', 'scripts/pkg/owner.py': '',
    }, ['scripts/pkg/owner.py'])
    with pytest.raises(AssertionError, match='scripts/pkg/__init__.py'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('source', [
    'import dependency', 'from dependency import VALUE',
    'def test_later():\n import dependency',
    'from scripts.dependency import VALUE',
])
def test_test_imported_product_source_is_required(tmp_path, source):
    manifest = _packet(tmp_path, {
        'tests/test_owner.py': source, 'scripts/dependency.py': 'VALUE = 1',
    }, ['tests/test_owner.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_shared_test_helper_and_its_product_dependency_are_required(tmp_path):
    manifest = _packet(tmp_path, {
        'tests/test_owner.py': 'from tests.helper import VALUE',
        'tests/helper.py': 'from dependency import VALUE',
        'scripts/dependency.py': 'VALUE = 1',
    }, ['tests/test_owner.py', 'tests/helper.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('source', ['from .helper import VALUE', 'from . import helper'])
def test_relative_test_helper_is_required(tmp_path, source):
    manifest = _packet(tmp_path, {
        'tests/pkg/__init__.py': '', 'tests/pkg/test_owner.py': source,
        'tests/pkg/helper.py': 'VALUE = 1',
    }, ['tests/pkg/__init__.py', 'tests/pkg/test_owner.py'])
    with pytest.raises(AssertionError, match='tests/pkg/helper.py'):
        measure_context(tmp_path, manifest)


def test_test_package_initializer_imports_are_followed(tmp_path):
    manifest = _packet(tmp_path, {
        'tests/pkg/__init__.py': 'import dependency',
        'tests/pkg/test_owner.py': '', 'scripts/dependency.py': '',
    }, ['tests/pkg/__init__.py', 'tests/pkg/test_owner.py'])
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_test_dependency_symlink_outside_root_is_refused(tmp_path):
    root = tmp_path / 'repo'
    outside = _write(tmp_path, 'outside.py', '')
    manifest = _packet(root, {'tests/test_owner.py': 'from tests import helper'}, ['tests/test_owner.py'])
    try:
        (root / 'tests/helper.py').symlink_to(outside)
    except OSError as exc:
        pytest.skip(f'symlink unavailable: {exc}')
    with pytest.raises(ValueError):
        measure_context(root, manifest)


def test_bootstrap_infrastructure_does_not_hide_explicit_test_imports(tmp_path):
    manifest = _packet(tmp_path, {
        'tests/conftest.py': 'import dependency', 'tests/test_owner.py': '',
        'scripts/dependency.py': '',
    }, ['tests/conftest.py', 'tests/test_owner.py'])
    assert measure_context(tmp_path, manifest)[0] > 0
    (tmp_path / 'tests/test_owner.py').write_text('import dependency')
    with pytest.raises(AssertionError, match='scripts/dependency.py'):
        measure_context(tmp_path, manifest)


def test_current_qualification_contract_cannot_be_omitted(tmp_path):
    manifest = _packet(tmp_path, {}, [])
    packet = json.loads(manifest.read_text(encoding='utf-8'))
    packet['files'].remove('docs/modules/context-tooling.md')
    manifest.write_text(json.dumps(packet))
    with pytest.raises(AssertionError, match='docs/modules/context-tooling.md'):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('name', [
    'AGENTS.md', 'CLAUDE.md', 'rules/development-laws.md',
    'docs/AGENTS.md', 'docs/STRUCTURE.md',
])
def test_complete_authority_files_cannot_be_replaced_by_record_views(tmp_path, name):
    manifest = _packet(tmp_path, {}, [])
    packet = json.loads(manifest.read_text(encoding='utf-8'))
    packet['files'] = [item for item in packet['files'] if item != name]
    manifest.write_text(json.dumps(packet))
    with pytest.raises(AssertionError, match=name):
        measure_context(tmp_path, manifest)


@pytest.mark.parametrize('path_type', ['PurePosixPath', 'PureWindowsPath'])
def test_relative_context_names_use_manifest_separators(path_type):
    import pathlib

    from tests.module_context import relative_context_names

    root = getattr(pathlib, path_type)('checkout')
    paths = {root / 'tests' / 'validator.py', root / 'docs' / 'records.json'}
    assert relative_context_names(root, paths) == ['docs/records.json', 'tests/validator.py']
