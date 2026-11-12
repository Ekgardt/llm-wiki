"""Replace a provider's source in a packet with its exact derived description."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from tests.module_context_paths import _contained
from tests.module_interface import SourceAccesses, packet_accesses
from tests.module_interface_render import patches_depend_on_internals, provider_contract

CONTRACT_PREFIX = '@contract/'


@dataclass(frozen=True)
class Dependency:
    provider: str
    names: tuple[str, ...]
    contract_sha256: str
    integration_tests: tuple[str, ...]


def declared_dependencies(manifest: dict) -> list[Dependency]:
    return [Dependency(entry['provider'], tuple(entry['names']), entry['contract_sha256'],
                       tuple(entry['integration_tests']))
            for entry in manifest.get('qualified_dependencies', ())]


def declared_providers(root: Path, manifest: dict) -> set[Path]:
    return {_contained(root, item.provider) for item in declared_dependencies(manifest)}


def contract_text(root: Path, provider: Path, names: set[str]) -> str:
    text, unknown = provider_contract(root, provider, names)
    assert not unknown, f'{provider.name}: names without a description: {sorted(unknown)}'
    return text


def contract_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def contract_entries(root: Path, manifest: dict) -> dict[str, bytes]:
    return {
        CONTRACT_PREFIX + item.provider:
            contract_text(root, _contained(root, item.provider), set(item.names)).encode('utf-8')
        for item in declared_dependencies(manifest)
    }


def _require_public_use(root: Path, provider: Path, accesses: SourceAccesses) -> None:
    assert provider not in accesses.opaque, f'{provider.name}: reached around its public names; keep its source'
    patched = accesses.patched.get(provider, set())
    accessed = accesses.accessed.get(provider, set())
    assert not patches_depend_on_internals(root, provider, patched, accessed), (
        f'{provider.name}: a stand-in depends on its internals; keep its source')


def _require_current(root: Path, provider: Path, item: Dependency) -> None:
    text = contract_text(root, provider, set(item.names))
    assert contract_digest(text) == item.contract_sha256, (
        f'{item.provider}: description differs from the code; run python -m tests.module_qualification_refresh')


def _require_evidence(root: Path, item: Dependency) -> None:
    assert item.integration_tests, f'{item.provider}: no real test crosses this boundary'
    missing = [name for name in item.integration_tests if not _contained(root, name).is_file()]
    assert not missing, f'{item.provider}: missing integration tests: {missing}'


def _require_dependency(root: Path, item: Dependency, accesses: SourceAccesses, files: set[Path]) -> None:
    provider = _contained(root, item.provider)
    assert provider not in files, f'{item.provider}: both full source and description in one packet'
    assert set(item.names) == accesses.accessed.get(provider, set()), f'{item.provider}: declared names differ from use'
    _require_public_use(root, provider, accesses)
    _require_current(root, provider, item)
    _require_evidence(root, item)


def validate_qualified(root: Path, manifest: dict, files: set[Path]) -> None:
    accesses = packet_accesses(root, files)
    for item in declared_dependencies(manifest):
        _require_dependency(root, item, accesses, files)
