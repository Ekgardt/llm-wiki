"""Read the explicit pilot and derive its real owned test inputs."""
from __future__ import annotations

import json
from pathlib import Path

from tests.module_context import context_files, measure_context
from tests.module_contract_calls import require_public_calls
from tests.module_contract_identity import _artifact, artifact_identity
from tests.module_contract_patches import require_test_public_calls
from tests.module_contract_surface import declared_kinds, require_declared_surface
from tests.module_ownership import ownership_report, tracked_files

_FIELDS = frozenset({'schema', 'provider_context', 'consumer_context', 'integration_context',
                     'provider_module', 'consumer_module', 'provider_source', 'consumer_source',
                     'interface', 'semantics', 'integration_tests', 'bindings'})
_ROLES = ('provider', 'consumer', 'integration')


def read_pilot(root: Path, path: Path) -> dict:
    spec = json.loads(path.read_bytes())
    if not isinstance(spec, dict) or set(spec) != _FIELDS:
        raise ValueError('Pilot declaration has missing or unknown fields')
    if spec['schema'] != 'dependency-contract-pilot/v1':
        raise ValueError('Unsupported pilot schema')
    _require_pilot_paths(root, spec)
    _require_module_sources(spec)
    return spec


def _require_pilot_paths(root: Path, spec: dict) -> None:
    names = [spec[f'{role}_context'] for role in _ROLES]
    names.extend(spec[name] for name in ('provider_source', 'consumer_source', 'interface', 'semantics'))
    _require_integration_list(spec['integration_tests'])
    for name in names + spec['integration_tests']:
        _artifact(root, name)
    if not _artifact(root, spec['semantics']).read_bytes().strip():
        raise ValueError('A public interface without semantics cannot qualify')


def _require_integration_list(names) -> None:
    if not isinstance(names, list) or not names:
        raise ValueError('Real integration evidence must be explicitly named')
    _require_test_names(names)
    if len(set(names)) != len(names):
        raise ValueError('Duplicate integration test selection')


def _require_test_names(names: list) -> None:
    if not all(isinstance(name, str) for name in names):
        raise ValueError('Integration test paths must be strings')


def _require_module_sources(spec: dict) -> None:
    for role in ('provider', 'consumer'):
        _require_module_source(spec[f'{role}_module'], spec[f'{role}_source'])
    if spec['provider_source'] == spec['consumer_source']:
        raise ValueError('Provider and consumer must be independent owners')
    _require_independent_contexts(spec)


def _require_independent_contexts(spec: dict) -> None:
    contexts = [spec[f'{role}_context'] for role in _ROLES]
    if len(set(contexts)) != len(contexts):
        raise ValueError('Provider, consumer and integration must have independent owners')


def _require_module_source(module: str, source: str) -> None:
    if not isinstance(module, str) or not all(part.isidentifier() for part in module.split('.')):
        raise ValueError('A Python module identity is required')
    if source != 'scripts/' + module.replace('.', '/') + '.py':
        raise ValueError('Module identity does not bind its declared source file')


def _owned_tests(root: Path, manifest: dict) -> set[str]:
    owned = context_files(root, manifest['ownership'])
    return {path.relative_to(root).as_posix() for path in owned if _test_file(path)}


def _test_file(path: Path) -> bool:
    return path.name.startswith('test_') and path.suffix == '.py'


def _packets(root: Path, spec: dict) -> tuple[dict, dict]:
    packets = {}
    measured = {}
    for role in _ROLES:
        path = _artifact(root, spec[f'{role}_context'])
        packets[role] = json.loads(path.read_bytes())
        measured[role] = measure_context(root, path)[0]
    return packets, measured


def _require_owner(ownership: dict, spec: dict, role: str, paths) -> None:
    owner = Path(spec[f'{role}_context']).name.removesuffix('-context.json')
    if any(ownership.get(name) != owner for name in paths):
        raise ValueError(f'Incomplete or wrong {role} ownership')


def _required_tests(root: Path, spec: dict, packets: dict, ownership: dict) -> list[str]:
    provider = _owned_tests(root, packets['provider'])
    consumer = _owned_tests(root, packets['consumer'])
    if not provider or not consumer:
        raise ValueError('Both real owners need their complete test selections')
    integration = spec['integration_tests']
    _require_owner(ownership, spec, 'integration', integration)
    return sorted(provider | consumer | set(integration))


def _require_bindings(root: Path, spec: dict, consumer_tests: set) -> dict:
    interface = _artifact(root, spec['interface']).read_text(encoding='utf-8')
    provider = _artifact(root, spec['provider_source']).read_text(encoding='utf-8')
    consumer = _artifact(root, spec['consumer_source']).read_text(encoding='utf-8')
    require_declared_surface(provider, interface)
    bindings = require_public_calls(consumer, spec['provider_module'], interface)
    if not bindings or bindings != spec['bindings']:
        raise ValueError('Declared binding does not match the real consumer')
    for name in sorted(consumer_tests):
        require_test_public_calls(_artifact(root, name).read_text(encoding='utf-8'),
                                  module=spec['provider_module'], interface=interface,
                                  consumer_module=spec['consumer_module'], consumer_source=consumer)
    return declared_kinds(interface)


def pilot_inputs(root: Path, path: Path) -> dict:
    """No success receipt is read; all source, ownership and context checks are fresh."""
    root = root.resolve()
    spec = read_pilot(root, path)
    packets, measured = _packets(root, spec)
    ownership = ownership_report(root, tracked=tracked_files(root))['ownership']
    _require_owner(ownership, spec, 'provider', [spec['provider_source'], spec['interface'], spec['semantics']])
    _require_owner(ownership, spec, 'consumer', [spec['consumer_source']])
    selected = _required_tests(root, spec, packets, ownership)
    kinds = _require_bindings(root, spec, _owned_tests(root, packets['consumer']))
    names = _input_names(root, path, packets)
    return {'spec': spec, 'tests': selected, 'kinds': kinds, 'packet_tokens': measured,
            'identity': artifact_identity(root, names)}


def _input_names(root: Path, path: Path, packets: dict) -> set[str]:
    files = set().union(*(context_files(root, manifest) for manifest in packets.values()))
    files.update({path.resolve(), root / 'uv.lock'})
    return {file.relative_to(root).as_posix() for file in files}
