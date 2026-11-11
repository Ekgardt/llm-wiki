"""A packet may hold a description of a provider only while that description is the truth."""
from __future__ import annotations

import json

import pytest

from tests.module_context import required_source_context
from tests.module_interface import packet_accesses, source_accesses
from tests.module_interface_render import provider_contract
from tests.module_qualification import (
    CONTRACT_PREFIX,
    contract_digest,
    contract_entries,
    contract_text,
    validate_qualified,
)
from tests.module_qualification_refresh import refresh_manifest

PROVIDER = '''"""A provider."""
import subprocess

LIMIT = 3


def _hidden():
    return 1


def visible(path: str) -> int:
    """Count a path."""
    if not path:
        raise ValueError("empty")
    subprocess.run(["true"], check=True)
    return _hidden() + LIMIT


def other() -> int:
    return visible("x")
'''


def _tree(root, consumer: str):
    (root / 'scripts').mkdir(parents=True, exist_ok=True)
    (root / 'tests').mkdir(parents=True, exist_ok=True)
    (root / 'scripts/provider.py').write_text(PROVIDER, encoding='utf-8')
    path = root / 'scripts/consumer.py'
    path.write_text(consumer, encoding='utf-8')
    (root / 'tests/test_edge.py').write_text('import consumer\n', encoding='utf-8')
    return root.resolve(), path.resolve()


def _manifest(root, names=('visible',)):
    text = contract_text(root, root / 'scripts/provider.py', set(names))
    return {'qualified_dependencies': [{
        'provider': 'scripts/provider.py', 'names': list(names),
        'contract_sha256': contract_digest(text), 'integration_tests': ['tests/test_edge.py'],
    }]}


def test_the_description_carries_call_shape_errors_and_effects(tmp_path):
    root, _ = _tree(tmp_path, 'from provider import visible\n')
    text, unknown = provider_contract(root, root / 'scripts/provider.py', {'visible'})
    assert unknown == set()
    assert 'def visible(path: str) -> int:' in text
    assert 'raises: ValueError' in text
    assert 'effects: process' in text
    assert 'other' not in text


def test_a_class_composed_from_another_file_is_described_with_what_it_inherits(tmp_path):
    root, _ = _tree(tmp_path, 'from composed import Composed\n')
    (root / 'scripts/parts.py').write_text(
        'class Part:\n    def visible_part(self) -> int:\n        """Inherited."""\n        return 1\n\n'
        '    def _hidden_part(self) -> int:\n        return 2\n', encoding='utf-8')
    (root / 'scripts/composed.py').write_text(
        'from parts import Part\n\n\nclass Composed(Part):\n    """Composed."""\n', encoding='utf-8')
    text, unknown = provider_contract(root, root / 'scripts/composed.py', {'Composed'})
    assert unknown == set()
    assert 'class Composed(Part):' in text
    assert 'def visible_part(self) -> int:' in text
    assert '_hidden_part' not in text


def test_a_constant_is_described_by_its_source_line(tmp_path):
    root, _ = _tree(tmp_path, 'from provider import LIMIT\n')
    assert 'LIMIT = 3' in contract_text(root, root / 'scripts/provider.py', {'LIMIT'})


def test_a_name_the_provider_does_not_have_has_no_description(tmp_path):
    root, _ = _tree(tmp_path, 'import provider\n')
    with pytest.raises(AssertionError, match='names without a description'):
        contract_text(root, root / 'scripts/provider.py', {'absent'})


def test_a_current_description_is_accepted_and_enters_the_packet(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible\n')
    manifest = _manifest(root)
    validate_qualified(root, manifest, {consumer})
    entries = contract_entries(root, manifest)
    assert b'def visible' in entries[CONTRACT_PREFIX + 'scripts/provider.py']


def test_a_changed_provider_fails_until_the_description_is_refreshed(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible\n')
    manifest = _manifest(root)
    provider = root / 'scripts/provider.py'
    provider.write_text(PROVIDER.replace('raise ValueError', 'raise OSError'), encoding='utf-8')
    with pytest.raises(AssertionError, match='description differs from the code'):
        validate_qualified(root, manifest, {consumer})


def test_the_refresh_command_repairs_a_stale_description(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible\n')
    manifest = {**_manifest(root), 'files': ['scripts/consumer.py']}
    (root / 'scripts/provider.py').write_text(PROVIDER.replace('raise ValueError', 'raise OSError'), encoding='utf-8')
    path = root / 'context.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    assert refresh_manifest(root, path) is True
    validate_qualified(root, json.loads(path.read_bytes()), {consumer})


def test_a_declaration_must_match_what_is_really_used(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible, other\n')
    with pytest.raises(AssertionError, match='declared names differ'):
        validate_qualified(root, _manifest(root), {consumer})


@pytest.mark.parametrize('consumer', [
    'import provider\nprovider._hidden()\n',
    'from provider import _hidden\n',
    'import provider\nhelper(provider)\n',
    'from provider import *\n',
    'import provider\ngetattr(provider, name)\n',
    'import provider\ngetattr(provider, "_hidden")\n',
])
def test_reaching_around_public_names_keeps_the_full_source(tmp_path, consumer):
    root, path = _tree(tmp_path, consumer)
    provider = root / 'scripts/provider.py'
    assert provider in source_accesses(root, path).opaque


@pytest.mark.parametrize('consumer', [
    'import provider\ngetattr(provider, "visible")\n',
    'import provider\nhasattr(provider, "visible")\n',
])
def test_a_read_by_a_constant_name_is_a_public_access(tmp_path, consumer):
    root, path = _tree(tmp_path, consumer)
    accesses = source_accesses(root, path)
    provider = root / 'scripts/provider.py'
    assert (provider in accesses.opaque, accesses.accessed[provider]) == (False, {'visible'})


def test_opaque_use_refuses_the_description(tmp_path):
    root, consumer = _tree(tmp_path, 'import provider\nprovider.visible("x")\nprovider._hidden()\n')
    manifest = _manifest(root, ('_hidden', 'visible'))
    with pytest.raises(AssertionError, match='around its public names'):
        validate_qualified(root, manifest, {consumer})


@pytest.mark.parametrize('body', [
    'monkeypatch.setattr(provider, "LIMIT", 9)',
    'monkeypatch.setattr(provider, "visible", stub)\nprovider.other()',
])
def test_a_stand_in_for_internals_refuses_the_description(tmp_path, body):
    root, consumer = _tree(tmp_path, f'import provider\ndef run(monkeypatch, stub):\n    {body.replace(chr(10), chr(10) + "    ")}\n')
    names = tuple(sorted(packet_accesses(root, {consumer}).accessed[root / 'scripts/provider.py']))
    with pytest.raises(AssertionError, match='depends on its internals'):
        validate_qualified(root, _manifest(root, names), {consumer})


def test_a_stand_in_for_a_public_callable_is_allowed(tmp_path):
    root, consumer = _tree(tmp_path, 'import provider\ndef run(monkeypatch, stub):\n    monkeypatch.setattr(provider, "visible", stub)\n')
    validate_qualified(root, _manifest(root), {consumer})


FACADE = 'from provider import LIMIT, visible\n\n\ndef wrapper() -> int:\n    return visible("x")\n'


def _facade_case(tmp_path, body: str):
    root, consumer = _tree(tmp_path, f'import facade\ndef run(monkeypatch, stub):\n    {body}\n')
    facade = root / 'scripts/facade.py'
    facade.write_text(FACADE, encoding='utf-8')
    names = sorted(packet_accesses(root, {consumer}).accessed[facade])
    text = contract_text(root, facade, set(names))
    manifest = {'qualified_dependencies': [{
        'provider': 'scripts/facade.py', 'names': names, 'contract_sha256': contract_digest(text),
        'integration_tests': ['tests/test_edge.py'],
    }]}
    return root, manifest, consumer


def test_a_stand_in_for_a_re_exported_public_callable_is_allowed(tmp_path):
    root, manifest, consumer = _facade_case(tmp_path, 'monkeypatch.setattr(facade, "visible", stub)')
    validate_qualified(root, manifest, {consumer})


@pytest.mark.parametrize('body', [
    'monkeypatch.setattr(facade, "visible", stub); facade.wrapper()',
    'monkeypatch.setattr(facade, "LIMIT", 9)',
])
def test_a_stand_in_for_a_re_export_the_test_also_runs_or_a_value_refuses(tmp_path, body):
    root, manifest, consumer = _facade_case(tmp_path, body)
    with pytest.raises(AssertionError, match='depends on its internals'):
        validate_qualified(root, manifest, {consumer})


def test_no_real_test_across_the_boundary_refuses_the_description(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible\n')
    manifest = _manifest(root)
    manifest['qualified_dependencies'][0]['integration_tests'] = []
    with pytest.raises(AssertionError, match='no real test crosses'):
        validate_qualified(root, manifest, {consumer})


def test_a_provider_cannot_be_both_described_and_included(tmp_path):
    root, consumer = _tree(tmp_path, 'from provider import visible\n')
    with pytest.raises(AssertionError, match='both full source and description'):
        validate_qualified(root, _manifest(root), {consumer, root / 'scripts/provider.py'})


def test_a_deliberately_unparsable_fixture_reaches_and_imports_nothing(tmp_path):
    """A syntax-error fixture is data: it cannot be imported, so it pulls no source in."""
    root, _ = _tree(tmp_path, 'from provider import visible\n')
    broken = root / 'tests/broken.py'
    broken.write_text('import provider\ndef broken(value: str) -> str\n', encoding='utf-8')
    accesses = source_accesses(root, broken)
    assert (accesses.accessed, accesses.opaque) == ({}, set())
    assert required_source_context(root, {broken}) == {broken}


LOADS_BY_PATH = 'from importlib.util import spec_from_file_location\nspec_from_file_location("loaded", PATH)\n'


def test_a_script_loaded_by_path_belongs_to_the_packet_of_the_file_that_loads_it(tmp_path):
    """Loading a script by its file path imports it, so its source is required."""
    root, consumer = _tree(tmp_path, '"""No import."""\n')
    runner = root / 'tests/test_runs.py'
    runner.write_text(
        'SCRIPT = "consumer.py"\nPATH = "scripts/provider.py"\nFIXTURE = "src/app.py"\n' + LOADS_BY_PATH,
        encoding='utf-8',
    )
    assert required_source_context(root, {runner}) == {runner, consumer, root / 'scripts/provider.py'}


def test_a_name_two_scripts_share_identifies_neither_but_a_path_does(tmp_path):
    """Path-building code naming `__init__.py` must not pull every package initializer in."""
    root, _ = _tree(tmp_path, '"""No import."""\n')
    (root / 'scripts/pkg').mkdir()
    twin = root / 'scripts/pkg/provider.py'
    twin.write_text('"""Twin."""\n', encoding='utf-8')
    runner = root / 'tests/test_runs.py'
    runner.write_text('PATH = "provider.py"\n' + LOADS_BY_PATH, encoding='utf-8')
    assert required_source_context(root, {runner}) == {runner}
    runner.write_text('PATH = "scripts/pkg/provider.py"\n' + LOADS_BY_PATH, encoding='utf-8')
    assert required_source_context(root, {runner}) == {runner, twin}


def test_a_string_that_cannot_be_a_path_names_no_script(tmp_path):
    root, _ = _tree(tmp_path, '"""No import."""\n')
    runner = root / 'tests/test_runs.py'
    runner.write_text(
        'BAD = "scripts/pro\\x00vider.py"\nUP = "../scripts/provider.py"\nPATH = UP\n' + LOADS_BY_PATH,
        encoding='utf-8',
    )
    assert required_source_context(root, {runner}) == {runner}


def test_a_script_named_as_data_launched_or_read_as_text_is_not_loaded(tmp_path):
    """Subprocess launches cross a command-line boundary; data paths and rule checks run nothing."""
    root, _ = _tree(tmp_path, '"""No import."""\n')
    runner = root / 'tests/test_runs.py'
    runner.write_text(
        'import subprocess\nfrom pathlib import Path\n'
        'subprocess.run(["python", "scripts/provider.py"])\n'
        'TEXT = Path("scripts/consumer.py").read_text()\nCHANGED = ["scripts/provider.py"]\n',
        encoding='utf-8',
    )
    assert required_source_context(root, {runner}) == {runner}


def test_a_script_loaded_by_path_is_reached_around_its_public_names(tmp_path):
    """A module loaded by path has no import name to follow, so nothing it uses can be listed."""
    root, _ = _tree(tmp_path, '"""No import."""\n')
    runner = root / 'tests/test_runs.py'
    runner.write_text('PATH = "scripts/provider.py"\n' + LOADS_BY_PATH, encoding='utf-8')
    assert root / 'scripts/provider.py' in source_accesses(root, runner).opaque


def test_a_benchmark_imported_by_name_is_reached_through_its_public_names(tmp_path):
    root, _ = _tree(tmp_path, '"""No import."""\n')
    (root / 'benchmark').mkdir()
    bench = root / 'benchmark/bench.py'
    bench.write_text('def run() -> int:\n    return 1\n', encoding='utf-8')
    runner = root / 'tests/test_bench.py'
    runner.write_text('import bench\nbench.run()\n', encoding='utf-8')
    accesses = source_accesses(root, runner)
    assert (bench in accesses.opaque, accesses.accessed[bench]) == (False, {'run'})


def test_a_name_both_launch_roots_carry_names_no_provider(tmp_path):
    root, _ = _tree(tmp_path, '"""No import."""\n')
    (root / 'benchmark').mkdir()
    (root / 'benchmark/provider.py').write_text('def visible() -> int:\n    return 2\n', encoding='utf-8')
    runner = root / 'tests/test_twins.py'
    runner.write_text('import provider\nprovider.visible()\n', encoding='utf-8')
    assert source_accesses(root, runner).accessed == {}


def test_a_name_re_exported_through_a_relative_import_is_described(tmp_path):
    root, _ = _tree(tmp_path, '')
    package = root / 'scripts/package'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'inner.py').write_text('try:\n    from ..provider import visible\nexcept ImportError:\n    pass\n', encoding='utf-8')
    (root / 'scripts/facade.py').write_text('from .package.inner import visible\n', encoding='utf-8')
    text, unknown = provider_contract(root, root / 'scripts/facade.py', {'visible'})
    assert (unknown, 'def visible(path: str) -> int' in text) == (set(), True)
