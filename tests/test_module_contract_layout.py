"""Explicit replacement preserves owned bytes; measuring it grants no execution pass."""
import json

import pytest
from llm_wiki.context_bundle import unpack_files

from tests.module_context import measure_context
from tests.module_contract_layout import (
    layout_entries,
    layout_files,
    measure_layout,
    serialize_layout,
)


@pytest.fixture
def packet(tmp_path):
    sources = dict.fromkeys([
        'AGENTS.md', 'CLAUDE.md', 'rules/development-laws.md', 'docs/AGENTS.md',
        'docs/STRUCTURE.md', 'tests/module_context.py', 'scripts/llm_wiki/context_bundle.py',
        'docs/modules/context-tooling.md'], '')
    sources.update({'scripts/provider.py': 'def value():\n    return 1\n',
                    'scripts/consumer.py': 'from provider import value\n',
                    'docs/interface.pyi': 'def value(): ...\n',
                    'docs/semantics.md': 'Returns integer one, without IO or state mutation.\n'})
    spec = {'schema': 'dependency-contract-pilot/v1', 'consumer_context': 'context.json',
            'provider_source': 'scripts/provider.py', 'interface': 'docs/interface.pyi',
            'semantics': 'docs/semantics.md'}
    sources['docs/dependency.json'] = json.dumps(spec)
    manifest = {'encoding': 'o200k_base', 'max_tokens': 100000,
                'serialization': 'exact-duplicates/v1',
                'files': [*sources, 'context.json'],
                'ownership': {'files': ['scripts/consumer.py', 'context.json']},
                'qualified_dependencies': ['docs/dependency.json']}
    sources['context.json'] = json.dumps(manifest)
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
    return tmp_path, tmp_path / 'context.json'


def _update(path, change):
    value = json.loads(path.read_bytes())
    change(value)
    path.write_text(json.dumps(value), encoding='utf-8')


def test_static_layout_is_exact_and_explicitly_unqualified(packet):
    root, path = packet
    manifest, entries, specs = layout_entries(root, path)
    bundle, tokens = serialize_layout(manifest, entries)
    assert unpack_files(bundle) == entries
    assert entries['scripts/consumer.py'] == (root / 'scripts/consumer.py').read_bytes()
    assert 'scripts/provider.py' not in entries
    assert {'docs/interface.pyi', 'docs/semantics.md', 'docs/dependency.json'} <= entries.keys()
    assert json.loads(entries['@qualification/binding.json'])['status'] == 'layout-only-unqualified'
    assert list(specs) == ['docs/dependency.json']
    assert measure_layout(root, path) == (tokens, len(entries))


def test_full_source_measurement_does_not_silently_accept_contract_mode(packet):
    with pytest.raises(AssertionError, match='fresh qualification'):
        measure_context(*packet)


def test_owned_provider_cannot_be_removed(packet):
    root, path = packet
    _update(path, lambda m: m['ownership']['files'].append('scripts/provider.py'))
    with pytest.raises(ValueError, match='consumer ownership'):
        layout_files(root, path)


def test_missing_transitive_source_remains_an_error(packet):
    root, path = packet
    (root / 'scripts/provider.py').write_text('import hidden\n', encoding='utf-8')
    (root / 'scripts/hidden.py').write_text('SECRET=1\n', encoding='utf-8')
    with pytest.raises(AssertionError, match='scripts/hidden.py'):
        layout_files(root, path)


def test_empty_semantics_refuse_even_a_static_layout(packet):
    root, path = packet
    (root / 'docs/semantics.md').write_bytes(b'')
    with pytest.raises(ValueError, match='Empty interface or semantics'):
        layout_files(root, path)


@pytest.mark.parametrize('names', [[], None, {}, [1], ['docs/dependency.json'] * 2])
def test_missing_or_ambiguous_declarations_refuse(packet, names):
    root, path = packet
    _update(path, lambda m: m.update(qualified_dependencies=names))
    with pytest.raises(ValueError):
        layout_files(root, path)


def test_declaration_cannot_disappear_from_the_packet(packet):
    root, path = packet
    _update(path, lambda m: m['files'].remove('docs/dependency.json'))
    with pytest.raises(ValueError, match='missing from verification'):
        layout_files(root, path)


def test_another_consumer_cannot_borrow_the_declaration(packet):
    root, path = packet
    (root / 'other.json').write_bytes(path.read_bytes())
    _update(root / 'docs/dependency.json', lambda s: s.update(consumer_context='other.json'))
    with pytest.raises(ValueError, match='another consumer'):
        layout_files(root, path)


def test_unused_provider_is_not_a_justified_omission(packet):
    root, path = packet
    (root / 'scripts/consumer.py').write_text('VALUE=1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='unused provider'):
        layout_files(root, path)


def test_unknown_serialization_cannot_be_ignored(packet):
    root, path = packet
    _update(path, lambda m: m.update(serialization='made-up'))
    with pytest.raises(AssertionError, match='exact-duplicates'):
        layout_files(root, path)


def test_actual_emitted_bytes_still_obey_the_original_ceiling(packet):
    root, path = packet
    manifest, entries, _ = layout_entries(root, path)
    entries['large-owned-source.txt'] = b'unique ordinary words ' * 100001
    with pytest.raises(ValueError, match='ceiling'):
        serialize_layout(manifest, entries)


def test_binding_record_cannot_overwrite_a_declared_file(packet):
    root, path = packet
    target = root / '@qualification/binding.json'
    target.parent.mkdir()
    target.write_bytes(b'owned original content')
    _update(path, lambda m: m['files'].append('@qualification/binding.json'))
    with pytest.raises(ValueError, match='overwrite'):
        layout_entries(root, path)
