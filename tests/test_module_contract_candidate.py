"""An experimental cut preserves the consumer and refuses other missing sources."""
import json

import pytest

from tests.module_contract_candidate import candidate_files


@pytest.fixture
def candidate(tmp_path):
    sources = {
        'scripts/provider.py': 'def value():\n    return 1\n',
        'scripts/consumer.py': 'from provider import value\n',
        'docs/modules/provider.pyi': 'def value(): ...\n',
        'docs/modules/provider.md': 'Returns one.\n',
        'docs/modules/contract-qualification.md': 'Experimental, fresh tests required.\n',
    }
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
    manifest = {'files': ['scripts/provider.py', 'scripts/consumer.py'],
                'ownership': {'files': ['scripts/consumer.py']}}
    path = tmp_path / 'docs/modules/consumer-context.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    inputs = {'spec': {'consumer_context': 'docs/modules/consumer-context.json',
                       'provider_source': 'scripts/provider.py',
                       'interface': 'docs/modules/provider.pyi', 'semantics': 'docs/modules/provider.md'}}
    return tmp_path, inputs, path


def test_only_the_explicit_provider_is_replaced(candidate):
    root, inputs, _ = candidate
    before = (root / 'scripts/consumer.py').read_bytes()
    _, files = candidate_files(root, inputs)
    assert root / 'scripts/consumer.py' in files
    assert (root / 'scripts/consumer.py').read_bytes() == before
    assert root / 'scripts/provider.py' not in files
    assert {root / 'docs/modules/provider.pyi', root / 'docs/modules/provider.md'} <= files


def test_consumer_owned_implementation_cannot_be_removed(candidate):
    root, inputs, path = candidate
    manifest = json.loads(path.read_bytes())
    manifest['ownership']['files'].append('scripts/provider.py')
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='consumer ownership'):
        candidate_files(root, inputs)


def test_another_omitted_dependency_refuses(candidate):
    root, inputs, _ = candidate
    (root / 'scripts/another.py').write_text('VALUE=2\n', encoding='utf-8')
    (root / 'scripts/consumer.py').write_text('from provider import value\nimport another\n',
                                             encoding='utf-8')
    with pytest.raises(ValueError, match='another source'):
        candidate_files(root, inputs)


def test_provider_transitive_implementation_is_not_silently_cut(candidate):
    root, inputs, _ = candidate
    (root / 'scripts/another.py').write_text('VALUE=2\n', encoding='utf-8')
    (root / 'scripts/provider.py').write_text('import another\n', encoding='utf-8')
    with pytest.raises(ValueError, match='another source'):
        candidate_files(root, inputs)
