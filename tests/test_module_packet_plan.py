"""A planned packet describes what it reaches publicly and holds whole what it reaches around."""
from __future__ import annotations

import json

from tests.module_context import context_reader
from tests.module_packet_plan import TOOLING, explained, planned_manifest, settled_plan

PROVIDER = '''"""A provider."""
import helper


def _hidden():
    return helper.value()


def visible() -> int:
    """The public door."""
    return _hidden()
'''


def _tree(root, consumer: str, *, test: str = 'from provider import visible\n'):
    for name in TOOLING:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('', encoding='utf-8')
    (root / 'scripts/provider.py').write_text(PROVIDER, encoding='utf-8')
    (root / 'scripts/helper.py').write_text('def value() -> int:\n    return 1\n', encoding='utf-8')
    (root / 'scripts/consumer.py').write_text(consumer, encoding='utf-8')
    (root / 'tests/test_provider.py').write_text(test, encoding='utf-8')
    manifest = root / 'docs/modules/consumer-context.json'
    owned = ['docs/modules/consumer-context.json', 'scripts/consumer.py']
    manifest.write_text(json.dumps({'encoding': 'o200k_base', 'max_tokens': 100000,
                                    'serialization': 'exact-duplicates/v1', 'files': owned,
                                    'ownership': {'files': owned}}), encoding='utf-8')
    return root.resolve(), manifest.resolve()


def _described(planned: dict) -> dict[str, list[str]]:
    return {d['provider']: d['integration_tests'] for d in planned.get('qualified_dependencies', ())}


def test_a_provider_reached_by_its_public_name_is_described_without_its_imports(tmp_path):
    root, manifest = _tree(tmp_path, 'from provider import visible\n')

    planned = planned_manifest(root, manifest, {})

    assert 'scripts/provider.py' not in planned['files']
    assert 'scripts/helper.py' not in planned['files']
    [dependency] = planned['qualified_dependencies']
    assert dependency['provider'] == 'scripts/provider.py'
    assert dependency['names'] == ['visible']
    assert dependency['integration_tests'] == ['tests/test_provider.py']


def test_a_provider_reached_around_its_public_names_is_held_whole_with_its_imports(tmp_path):
    root, manifest = _tree(tmp_path, 'from provider import _hidden\n')

    planned = planned_manifest(root, manifest, {})

    assert 'scripts/provider.py' in planned['files']
    assert _described(planned) == {'scripts/helper.py': ['tests/test_provider.py']}


def test_a_provider_no_test_reaches_is_held_whole(tmp_path):
    root, manifest = _tree(tmp_path, 'from provider import visible\n', test='')

    planned = planned_manifest(root, manifest, {})

    assert 'scripts/provider.py' in planned['files']


FACADE = 'from helper import value\n\n\ndef own() -> int:\n    return 2\n'


def _facade_tree(root, test: str):
    root, manifest = _tree(root, 'from helper import value\n', test=test)
    (root / 'scripts/facade.py').write_text(FACADE, encoding='utf-8')
    return root, manifest


def test_a_test_through_a_facade_crosses_the_module_the_name_was_taken_from(tmp_path):
    root, manifest = _facade_tree(tmp_path, 'from facade import value\n')

    planned = planned_manifest(root, manifest, {})

    [dependency] = planned['qualified_dependencies']
    assert dependency['provider'] == 'scripts/helper.py'
    assert dependency['integration_tests'] == ['tests/test_provider.py']


def test_a_test_crosses_what_the_public_call_runs_in_another_module(tmp_path):
    root, manifest = _tree(tmp_path, 'from helper import value\n')

    planned = planned_manifest(root, manifest, {})

    assert _described(planned) == {'scripts/helper.py': ['tests/test_provider.py']}


def test_a_facade_name_of_its_own_is_no_evidence_for_what_it_imports(tmp_path):
    root, manifest = _facade_tree(tmp_path, 'from facade import own\n')

    planned = planned_manifest(root, manifest, {})

    assert 'scripts/helper.py' in planned['files']


def test_a_declared_directory_is_kept_and_its_files_are_owned_not_listed(tmp_path):
    root, manifest = _tree(tmp_path, 'from provider import visible\n')
    (root / 'tests/modules/consumer').mkdir(parents=True)
    (root / 'tests/modules/consumer/test_consumer.py').write_text('import helper\n', encoding='utf-8')
    data = json.loads(manifest.read_bytes())
    data['directories'] = data['ownership']['directories'] = ['tests/modules/consumer']
    manifest.write_text(json.dumps(data), encoding='utf-8')

    planned = planned_manifest(root, manifest, {})

    assert planned['directories'] == ['tests/modules/consumer']
    assert 'tests/modules/consumer/test_consumer.py' not in planned['files']
    assert 'scripts/helper.py' in _described(planned)


def test_a_declared_run_time_read_survives_a_replan_and_is_readable(tmp_path):
    root, manifest = _tree(tmp_path, 'from provider import visible\n')
    (root / 'benchmark').mkdir()
    (root / 'benchmark/gold.json').write_text('{}', encoding='utf-8')
    data = json.loads(manifest.read_bytes())
    data['reads'] = ['benchmark/gold.json']
    manifest.write_text(json.dumps(data), encoding='utf-8')

    planned = planned_manifest(root, manifest, {})
    manifest.write_text(json.dumps(planned), encoding='utf-8')
    replanned = planned_manifest(root, manifest, {})

    assert replanned['reads'] == ['benchmark/gold.json']
    assert 'benchmark/gold.json' in replanned['files']
    assert context_reader(root, manifest)(root / 'benchmark/gold.json') == '{}'


def test_the_explanation_names_the_file_that_reached_around(tmp_path):
    root, manifest = _tree(tmp_path, 'import provider\nprovider._hidden()\n')

    plan, _manifest = settled_plan(root, manifest, {})

    assert any("scripts/provider.py: reached around its public names by ['scripts/consumer.py']" in line
               for line in explained(plan))
