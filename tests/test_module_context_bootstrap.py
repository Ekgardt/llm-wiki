"""Implicit pytest loaders cannot disappear from a maintenance packet."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
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
    tree = ast.parse((ROOT / 'tests/module_context.py').read_text(encoding='utf-8'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    roots = set().union(*(_import_roots(node) for node in nodes))
    assert all(_allowed_measurement_import(name) for name in roots)


def _import_roots(node):
    import ast
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0
        return {node.module or ''}
    return {a.name for a in node.names}


def _allowed_measurement_import(name):
    import sys
    helpers = {'tests.module_context_paths', 'tests.module_records', 'tests.module_record_context'}
    return name in helpers | {'llm_wiki.context_bundle'} or name.split('.')[0] in sys.stdlib_module_names | {'tiktoken'}


def test_codec_allowance_does_not_allow_any_other_product_module():
    assert _allowed_measurement_import('llm_wiki.context_bundle')
    assert not _allowed_measurement_import('llm_wiki.queue_child_runtime')
    assert not _allowed_measurement_import('memory_queue')


def test_compact_measurement_refuses_a_lossy_codec(tmp_path, monkeypatch):
    from tests import module_context
    path = _write(tmp_path, 'content.py', 'data = 1')
    monkeypatch.setattr(module_context, 'unpack_files', lambda bundle: {})
    with pytest.raises(AssertionError, match='lost file content'):
        module_context.compact_bundle(tmp_path, {path})


def test_codec_source_is_required_in_measurement_packet(tmp_path):
    from tests.module_context import measure_context
    helper = _write(tmp_path, 'tests/module_context.py', '')
    _write(tmp_path, 'scripts/llm_wiki/context_bundle.py', '')
    manifest = _write(tmp_path, 'context.json', json.dumps({
        'encoding': 'o200k_base', 'max_tokens': 100000,
        'serialization': 'exact-duplicates/v1',
        'files': ['context.json', str(helper.relative_to(tmp_path))]}))
    with pytest.raises(AssertionError, match='Missing required context'):
        measure_context(tmp_path, manifest)


def test_unknown_serialization_is_not_silently_treated_as_raw(tmp_path):
    from tests.module_context import _manifest_bundle
    with pytest.raises(AssertionError, match='Unknown maintenance serialization'):
        _manifest_bundle(tmp_path, set(), {'serialization': 'made-up'})


@pytest.mark.parametrize('name', ['module_context_paths', 'module_records', 'module_record_context'])
def test_record_measurement_helpers_cannot_import_product_executors(name):
    import ast
    tree = ast.parse((ROOT / 'tests' / f'{name}.py').read_text(encoding='utf-8'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    roots = set().union(*(_import_roots(node) for node in nodes))
    assert all(_allowed_measurement_import(item) for item in roots)
@pytest.mark.parametrize('declaration', [[], ['dependency.json'], None])
def test_full_source_serializer_cannot_ignore_qualified_declaration(tmp_path, declaration):
    from tests.module_context import _manifest_bundle

    with pytest.raises(AssertionError, match='fresh qualification'):
        _manifest_bundle(tmp_path, set(), {'qualified_dependencies': declaration})
