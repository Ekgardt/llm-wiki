"""Build an explicitly experimental packet only after a fresh real qualification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import tiktoken
from llm_wiki.context_bundle import pack_files, unpack_files

from tests.module_context import context_entries, context_files, required_source_context
from tests.module_contract_identity import require_artifact_identity
from tests.module_contract_qualification import qualify


def candidate_files(root: Path, inputs: dict) -> tuple[dict, set[Path]]:
    spec = inputs['spec']
    manifest = json.loads((root / spec['consumer_context']).read_bytes())
    original = context_files(root, manifest)
    provider = root / spec['provider_source']
    owned = context_files(root, manifest['ownership'])
    if provider not in original or provider in owned:
        raise ValueError('Only a dependency outside consumer ownership may be replaced')
    files = original - {provider}
    files.update(root / spec[name] for name in ('interface', 'semantics'))
    files.add(root / 'docs/modules/contract-qualification.md')
    _require_remaining_sources(root, files, provider)
    return manifest, files


def _require_remaining_sources(root: Path, files: set[Path], provider: Path) -> None:
    missing = required_source_context(root, files) - files
    if missing != {provider}:
        raise ValueError('Experimental packet omits another source dependency')


def _binding_record(root: Path, inputs: dict, evidence: Path) -> bytes:
    spec = inputs['spec']
    names = [spec[name] for name in ('provider_source', 'interface', 'semantics')]
    record = {'schema': 'experimental-dependency-binding/v1',
              'status': 'paired-task-required-not-adopted',
              'provider_context': spec['provider_context'], 'bindings': spec['bindings'],
              'artifacts': {name: inputs['identity']['files'][name] for name in names},
              'evidence': {'path': evidence.relative_to(root).as_posix(),
                           'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    return json.dumps(record, sort_keys=True, indent=2).encode('utf-8')


def _serialize(root: Path, inputs: dict, output: Path) -> tuple[str, int]:
    manifest, files = candidate_files(root, inputs)
    entries = context_entries(root, files, manifest)
    entries['@experiment/dependency-binding.json'] = _binding_record(root, inputs, output / 'experiment.json')
    bundle = pack_files(entries)
    if unpack_files(bundle) != entries:
        raise ValueError('Experimental packet lost bytes')
    tokens = len(tiktoken.get_encoding(manifest['encoding']).encode_ordinary(bundle))
    if tokens > manifest['max_tokens']:
        raise ValueError('Experimental packet exceeds the unchanged owner ceiling')
    return bundle, tokens


def build_candidate(root: Path, pilot: Path, output: Path) -> dict:
    """No pre-existing success record can be supplied in place of execution."""
    root = root.resolve()
    output = output.resolve()
    if not output.is_relative_to(root):
        raise ValueError('Derived evidence must have a checkout-relative location')
    result = qualify(root, pilot, output)
    inputs = result['inputs']
    bundle, tokens = _serialize(root, inputs, output)
    identity = inputs['identity']
    require_artifact_identity(root, set(identity['files']), identity)
    (output / 'candidate.bundle').write_text(bundle, encoding='utf-8', newline='')
    comparison = {'status': 'paired-task-required-not-adopted',
                  'full_source_tokens': inputs['packet_tokens']['consumer'],
                  'experimental_tokens': tokens, 'full_cycle_savings': None}
    (output / 'comparison.json').write_text(json.dumps(comparison, indent=2), encoding='utf-8')
    return comparison
