"""Static qualified layout: complete verification inputs, never execution evidence."""
from __future__ import annotations

import json
from pathlib import Path

import tiktoken
from llm_wiki.context_bundle import pack_files, unpack_files

from tests.module_context import (
    context_entries,
    context_files,
    required_source_context,
    validate_context,
)
from tests.module_contract_identity import _artifact, artifact_identity


def dependency_names(manifest: dict) -> list[str]:
    names = manifest.get('qualified_dependencies')
    if not isinstance(names, list) or not names:
        raise ValueError('Qualified dependencies require an explicit nonempty list')
    _require_unique_names(names)
    return names


def _require_unique_names(names: list) -> None:
    if not all(isinstance(name, str) for name in names):
        raise ValueError('Dependency declarations must name files')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate dependency declaration')


def replace_provider(root: Path, manifest: dict, files: set[Path], spec: dict) -> set[Path]:
    provider = _artifact(root, spec['provider_source'])
    owned = context_files(root, manifest['ownership'])
    if provider not in files or provider in owned:
        raise ValueError('Only a dependency outside consumer ownership may be replaced')
    contracts = _contracts(root, spec)
    return (files - {provider}) | contracts


def _contracts(root: Path, spec: dict) -> set[Path]:
    contracts = {_artifact(root, spec[name]) for name in ('interface', 'semantics')}
    if not all(path.read_bytes().strip() for path in contracts):
        raise ValueError('Empty interface or semantics cannot define a qualified layout')
    return contracts


def _spec(root: Path, name: str, consumer: Path, inputs: set[Path]) -> dict:
    path = _artifact(root, name)
    if path not in inputs:
        raise ValueError('Dependency declaration is missing from verification inputs')
    spec = json.loads(path.read_bytes())
    _require_consumer(root, consumer, spec)
    return spec


def _require_consumer(root: Path, consumer: Path, spec: dict) -> None:
    if spec['schema'] != 'dependency-contract-pilot/v1':
        raise ValueError('Unsupported dependency declaration schema')
    if _artifact(root, spec['consumer_context']) != consumer:
        raise ValueError('Declaration belongs to another consumer')


def layout_files(root: Path, manifest_path: Path) -> tuple[dict, set[Path], dict]:
    root, manifest_path = root.resolve(), manifest_path.resolve()
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest.get('serialization') == 'exact-duplicates/v1', 'Qualified layout requires exact-duplicates/v1'
    inputs = validate_context(root, manifest_path, manifest)
    specs = {name: _spec(root, name, manifest_path, inputs) for name in dependency_names(manifest)}
    files = inputs
    for spec in specs.values():
        files = replace_provider(root, manifest, files, spec)
    providers = {_artifact(root, spec['provider_source']) for spec in specs.values()}
    if required_source_context(root, files) - files != providers:
        raise ValueError('Qualified layout omits another source or an unused provider')
    return manifest, files, specs


def layout_entries(root: Path, manifest_path: Path) -> tuple[dict, dict, dict]:
    root = root.resolve()
    manifest, files, specs = layout_files(root, manifest_path)
    entries = context_entries(root, files, manifest)
    names = {spec[key] for spec in specs.values()
             for key in ('provider_source', 'interface', 'semantics')}
    record = {'schema': 'qualified-dependency-layout/v1', 'status': 'layout-only-unqualified',
              'declarations': sorted(specs), 'artifacts': artifact_identity(root, names)}
    add_binding(entries, '@qualification/binding.json', json.dumps(record, sort_keys=True, indent=2).encode('utf-8'))
    return manifest, entries, specs


def add_binding(entries: dict[str, bytes], name: str, content: bytes) -> None:
    if name in entries:
        raise ValueError('Binding evidence would overwrite a declared file')
    entries[name] = content


def serialize_layout(manifest: dict, entries: dict[str, bytes]) -> tuple[str, int]:
    bundle = pack_files(entries)
    if unpack_files(bundle) != entries:
        raise ValueError('Qualified layout lost exact paths or bytes')
    tokens = len(tiktoken.get_encoding(manifest['encoding']).encode_ordinary(bundle))
    if tokens > manifest['max_tokens']:
        raise ValueError(f'Qualified layout exceeds owner ceiling: {tokens}')
    return bundle, tokens


def measure_layout(root: Path, manifest_path: Path) -> tuple[int, int]:
    """No tests or receipt are accepted here; the result is only a layout size."""
    manifest, entries, _specs = layout_entries(root, manifest_path)
    _bundle, tokens = serialize_layout(manifest, entries)
    return tokens, len(entries)
