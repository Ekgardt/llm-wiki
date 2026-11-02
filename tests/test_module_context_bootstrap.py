"""Implicit pytest loaders cannot disappear from a maintenance packet."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = sorted((ROOT / 'docs/modules').glob('*-context.json'))


@pytest.mark.parametrize('path', MANIFESTS, ids=lambda p: p.stem)
def test_existing_packets_include_actual_pytest_bootstrap(path):
    manifest = json.loads(path.read_text())
    assert {'tests/conftest.py', 'tests/code_kernel_helpers.py'} <= set(manifest['files'])


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_ancestor_conftest_and_recursive_plugin_are_required(tmp_path):
    from tests.module_context import required_bootstrap
    test = _write(tmp_path, 'tests/modules/example/test_unit.py', '')
    root = _write(tmp_path, 'tests/conftest.py', 'pytest_plugins = ("tests.plugin",)')
    local = _write(tmp_path, 'tests/modules/conftest.py', '')
    plugin = _write(tmp_path, 'tests/plugin.py', 'pytest_plugins = "tests.next_plugin"')
    chained = _write(tmp_path, 'tests/next_plugin.py', 'pytest_plugins = ["tests.plugin"]')
    assert required_bootstrap(tmp_path, {test}) == {root, local, plugin, chained}


def test_dynamic_plugin_registration_is_not_silently_ignored(tmp_path):
    from tests.module_context import required_bootstrap
    test = _write(tmp_path, 'tests/test_unit.py', '')
    _write(tmp_path, 'tests/conftest.py', 'pytest_plugins = choose_plugins()')
    with pytest.raises(ValueError):
        required_bootstrap(tmp_path, {test})


def test_conditional_local_plugin_is_counted_conservatively(tmp_path):
    from tests.module_context import required_bootstrap
    test = _write(tmp_path, 'tests/test_unit.py', '')
    conf = _write(tmp_path, 'tests/conftest.py', 'if True:\n    pytest_plugins = "tests.plugin"')
    plugin = _write(tmp_path, 'tests/plugin.py', '')
    assert required_bootstrap(tmp_path, {test}) == {conf, plugin}


def test_undeclared_plugin_file_is_named(tmp_path):
    from tests.module_context import required_bootstrap
    test = _write(tmp_path, 'tests/test_unit.py', '')
    _write(tmp_path, 'tests/conftest.py', 'pytest_plugins = "tests.missing"')
    with pytest.raises(AssertionError, match='Missing local pytest plugin'):
        required_bootstrap(tmp_path, {test})


def test_directory_expansion_does_not_ignore_new_test_files(tmp_path):
    from tests.module_context import context_files
    first = _write(tmp_path, 'tests/modules/a/test_first.py', 'a = 1')
    second = _write(tmp_path, 'tests/modules/a/test_second.py', 'b = 2')
    assert context_files(tmp_path, {'files': [], 'directories': ['tests/modules/a']}) == {first, second}


def test_missing_declared_file_is_a_failure(tmp_path):
    from tests.module_context import context_files
    with pytest.raises(AssertionError, match='Missing declared context file'):
        context_files(tmp_path, {'files': ['absent.py']})


def test_outside_path_is_not_counted_as_a_module_file(tmp_path):
    from tests.module_context import context_files
    with pytest.raises(ValueError):
        context_files(tmp_path, {'files': ['../outside.py']})


def test_serialization_preserves_text_and_counts_framing(tmp_path):
    from tests.module_context import serialized_bundle
    path = _write(tmp_path, 'hello.py', 'Привет\nFILE another 0 bytes\n')
    raw = path.read_bytes()
    expected = f'FILE hello.py {len(raw)} bytes\n' + raw.decode() + '\n'
    assert serialized_bundle(tmp_path, {path}) == expected


def test_incomplete_manifest_fails_before_budget_success(tmp_path):
    from tests.module_context import measure_context
    _write(tmp_path, 'tests/conftest.py', '')
    test = _write(tmp_path, 'tests/test_unit.py', '')
    manifest = _write(tmp_path, 'context.json', json.dumps({
        'encoding': 'o200k_base', 'max_tokens': 100000, 'files': [str(test.relative_to(tmp_path))]}))
    with pytest.raises(AssertionError, match='Missing required context'):
        measure_context(tmp_path, manifest)


def test_measurement_helper_cannot_import_product_executors():
    import ast
    import sys
    tree = ast.parse((ROOT / 'tests/module_context.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    roots = set().union(*(_import_roots(node) for node in nodes))
    assert roots <= sys.stdlib_module_names | {'tiktoken'}


def _import_roots(node):
    import ast
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0
        return {(node.module or '').split('.')[0]}
    return {a.name.split('.')[0] for a in node.names}
