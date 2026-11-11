"""Re-plan a module's maintenance packet from the files it owns.

A packet holds the module's own files whole. Every other source it needs is held
either whole or, when the module reaches it only through its public names, as its
exact derived description. The plan grows to the fixed point the measurement
checks, so a written plan is one `measure_context` accepts or one it refuses on
size alone.

Usage: python -m tests.module_packet_plan [--write] docs/modules/<name>-context.json ...
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tests.module_contract_docs_helpers import _required_guides
from tests.module_interface import SourceAccesses, is_provider_source, source_accesses
from tests.module_interface_render import (
    names_run_by,
    patches_depend_on_internals,
    provider_contract,
)
from tests.module_qualification import contract_digest
from tests.module_record_context import context_record_files, selected_entries

PLANNED_KEYS = frozenset({'encoding', 'max_tokens', 'serialization', 'files', 'directories', 'ownership',
                          'reads', 'qualified_dependencies', 'records'})
TOOLING = ('AGENTS.md', 'CLAUDE.md', 'rules/development-laws.md', 'docs/AGENTS.md',
           'docs/STRUCTURE.md', 'tests/module_context.py', 'scripts/llm_wiki/context_bundle.py',
           'docs/modules/context-tooling.md')
# The evidence count the existing manifests settled on: enough to show that real tests
# cross the boundary without making every description depend on a long test list.
EVIDENCE_TESTS = 3


@dataclass
class Plan:
    root: Path
    owned: set[Path]
    ownership: dict[str, str]
    record_names: tuple[str, ...]
    old_evidence: dict[str, list[str]]
    records: list[dict]
    whole: set[Path] = field(default_factory=set)
    described: dict[Path, set[str]] = field(default_factory=dict)


@functools.cache
def _accesses_of(root: Path, source: Path) -> SourceAccesses:
    return source_accesses(root, source)


def _packet_accesses(root: Path, files: set[Path]) -> SourceAccesses:
    found = SourceAccesses()
    for path in sorted(files):
        if path.suffix == '.py':
            found.merge(_accesses_of(root, path))
    return found


@functools.cache
def _owners_reached(root: Path, provider: Path, names: frozenset[str]) -> dict[Path, frozenset[str]]:
    """The providers a call to `names` runs, through a re-export or the code it calls."""
    run = names_run_by(root, provider, names)
    borrowed = {owner: frozenset(run & taken) for owner, taken in _accesses_of(root, provider).accessed.items()}
    return {owner: taken for owner, taken in borrowed.items() if taken}


def _grow(reached: dict[Path, set[str]], owner: Path, names: frozenset[str]) -> bool:
    known = reached.setdefault(owner, set())
    added = names - known
    known |= added
    return bool(added)


def _reached_providers(root: Path, test: Path) -> list[Path]:
    reached: dict[Path, set[str]] = {p: set(n) for p, n in _accesses_of(root, test).accessed.items()}
    pending = list(reached)
    while pending:
        provider = pending.pop()
        owners = _owners_reached(root, provider, frozenset(reached[provider])).items()
        pending.extend(owner for owner, names in owners if _grow(reached, owner, names))
    return sorted(reached)


@functools.cache
def _tests_reaching(root: Path) -> dict[Path, tuple[str, ...]]:
    """Each provider, and the tests that reach it through an import, a patch, a re-export or a call."""
    found: dict[Path, list[str]] = {}
    for test in sorted((root / 'tests').rglob('test_*.py')):
        for provider in _reached_providers(root, test):
            found.setdefault(provider, []).append(test.relative_to(root).as_posix())
    return {provider: tuple(tests) for provider, tests in found.items()}


def _evidence(plan: Plan, provider: Path) -> list[str]:
    name = provider.relative_to(plan.root).as_posix()
    kept = [test for test in plan.old_evidence.get(name, ()) if (plan.root / test).is_file()]
    return kept or list(_tests_reaching(plan.root).get(provider, ())[:EVIDENCE_TESTS])


def _describable(plan: Plan, provider: Path, accesses: SourceAccesses) -> bool:
    if not is_provider_source(plan.root, provider) or provider in accesses.opaque:
        return False
    accessed = accesses.accessed.get(provider, set())
    if patches_depend_on_internals(plan.root, provider, accesses.patched.get(provider, set()), accessed):
        return False
    _text, unknown = provider_contract(plan.root, provider, accessed)
    return not unknown and bool(_evidence(plan, provider))


def _files(plan: Plan) -> set[Path]:
    return plan.owned | plan.whole | {plan.root / name for name in TOOLING}


@functools.cache
def _direct_dependencies(root: Path, source: Path) -> frozenset[Path]:
    from tests.module_context import _source_dependencies

    return frozenset(_source_dependencies(root, source))


def _required(plan: Plan, files: set[Path]) -> set[Path]:
    """One layer at a time, so a provider is described before its own imports are asked for."""
    from tests.module_context import required_bootstrap

    needed = required_bootstrap(plan.root, files)
    needed |= set().union(*(_direct_dependencies(plan.root, p) for p in files if p.suffix == '.py'))
    needed |= _required_guides(plan.root, files, plan.ownership, record_names=plan.record_names)
    return needed | context_record_files(plan.root, {'records': plan.records})


def _keep_describable(plan: Plan, accesses: SourceAccesses, files: set[Path]) -> bool:
    """Move each described provider the packet now reaches around back to whole source."""
    lost = {p for p in plan.described if p in files or not _describable(plan, p, accesses)}
    for provider in lost:
        del plan.described[provider]
        plan.whole.add(provider)
    return bool(lost)


def _admit(plan: Plan, missing: set[Path], accesses: SourceAccesses) -> None:
    for provider in sorted(missing):
        if _describable(plan, provider, accesses):
            plan.described[provider] = accesses.accessed.get(provider, set())
        else:
            plan.whole.add(provider)


def _step(plan: Plan) -> bool:
    files = _files(plan)
    accesses = _packet_accesses(plan.root, files)
    if _keep_describable(plan, accesses, files):
        return True
    missing = _required(plan, files) - files - set(plan.described)
    _admit(plan, missing, accesses)
    return bool(missing)


def _settled(plan: Plan) -> Plan:
    while _step(plan):
        pass
    accesses = _packet_accesses(plan.root, _files(plan))
    plan.described = {p: accesses.accessed.get(p, set()) for p in plan.described}
    return plan


def _dependency(plan: Plan, provider: Path, names: set[str]) -> dict:
    text, _unknown = provider_contract(plan.root, provider, names)
    return {'provider': provider.relative_to(plan.root).as_posix(), 'names': sorted(names),
            'contract_sha256': contract_digest(text), 'integration_tests': _evidence(plan, provider)}


def settled_plan(root: Path, manifest_path: Path, ownership: dict[str, str]) -> tuple[Plan, dict]:
    manifest = json.loads(manifest_path.read_bytes())
    unplanned = set(manifest) - PLANNED_KEYS
    assert not unplanned, f'{manifest_path.name}: keys this planner does not derive: {sorted(unplanned)}'
    old = {e['provider']: e['integration_tests'] for e in manifest.get('qualified_dependencies', ())}
    owned = {root / name for name in manifest['ownership']['files']} | _in_directories(root, manifest['ownership'])
    plan = _settled(Plan(root, owned | {manifest_path.resolve()}, ownership,
                         tuple(selected_entries(root, manifest)), old, manifest.get('records', []),
                         whole=_declared_reads(root, manifest)))
    return plan, manifest


def _declared_reads(root: Path, manifest: dict) -> set[Path]:
    """Files a test opens at run time by a path no import names, which no analysis can find."""
    from tests.module_context import context_files

    return context_files(root, {'files': manifest.get('reads', [])})


def _in_directories(root: Path, declared: dict) -> set[Path]:
    """The files a declared directory holds, which the measurement counts as declared."""
    from tests.module_context import context_files

    return context_files(root, {'files': [], 'directories': declared.get('directories', [])})


def planned_manifest(root: Path, manifest_path: Path, ownership: dict[str, str]) -> dict:
    return _manifest(*settled_plan(root, manifest_path, ownership))


def _manifest(plan: Plan, manifest: dict) -> dict:
    head = _present(manifest, ('encoding', 'max_tokens', 'serialization'))
    tail = _present(manifest, ('ownership', 'reads', 'records'))
    listed = _files(plan) - _in_directories(plan.root, manifest)
    files = sorted(p.relative_to(plan.root).as_posix() for p in listed)
    return {**head, 'files': files, **_present(manifest, ('directories',)), **tail, **_qualified(plan)}


def _present(manifest: dict, keys: tuple[str, ...]) -> dict:
    return {key: manifest[key] for key in keys if key in manifest}


def _qualified(plan: Plan) -> dict:
    if not plan.described:
        return {}
    return {'qualified_dependencies': [_dependency(plan, p, n) for p, n in sorted(plan.described.items())]}


def _replace_whole(path: Path, data: bytes) -> None:
    """Swap the file in one rename, so a planner running beside this one never reads half of it."""
    staged = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    staged.write_bytes(data)
    os.replace(staged, path)


def _measured(root: Path, path: Path, manifest: dict) -> tuple[str, int | None]:
    """Measure a candidate manifest in place, putting the original back when it is refused."""
    from tests.module_context import measure_context

    original = path.read_bytes()
    _replace_whole(path, (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
    try:
        return 'OK', measure_context(root, path)[0]
    except AssertionError as error:
        _replace_whole(path, original)
        return f'REFUSED {str(error)[:300]}', None


def _ownership(root: Path) -> dict[str, str]:
    from tests.module_ownership import ownership_report

    report = ownership_report(root)
    return {**report['ownership'], **report['record_ownership']}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='keep each plan that measures within its limit')
    parser.add_argument('manifests', nargs='+', type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    ownership = _ownership(root)
    refused = 0
    for path in args.manifests:
        refused += _report(root, path.resolve(), ownership, keep=args.write)
    return int(bool(refused))


def _report(root: Path, path: Path, ownership: dict[str, str], *, keep: bool) -> int:
    original = path.read_bytes()
    plan, manifest = settled_plan(root, path, ownership)
    verdict, tokens = _measured(root, path, _manifest(plan, manifest))
    if not keep and tokens is not None:
        _replace_whole(path, original)
    print(path.name, verdict, tokens or '', flush=True)
    if tokens is None:
        print(*explained(plan), sep='\n', flush=True)
    return int(tokens is None)


def _tokens(path: Path) -> int:
    from tests.module_context import count_tokens

    return count_tokens(path.read_text(encoding='utf-8'))


def _reached_around_by(plan: Plan, provider: Path) -> list[str]:
    files = _files(plan)
    return sorted(p.relative_to(plan.root).as_posix() for p in files
                  if p.suffix == '.py' and provider in _accesses_of(plan.root, p).opaque)


def _why_whole(plan: Plan, provider: Path) -> str:
    if not is_provider_source(plan.root, provider):
        return 'not a launch-root source'
    around = _reached_around_by(plan, provider)
    if around:
        return f'reached around its public names by {around[:4]}'
    return _undescribable_because(plan, provider, _packet_accesses(plan.root, _files(plan)))


def _undescribable_because(plan: Plan, provider: Path, accesses: SourceAccesses) -> str:
    accessed = accesses.accessed.get(provider, set())
    patched = accesses.patched.get(provider, set())
    if patches_depend_on_internals(plan.root, provider, patched, accessed):
        return f'a stand-in depends on its internals: patched {sorted(patched)[:6]}'
    _text, unknown = provider_contract(plan.root, provider, accessed)
    if unknown:
        return f'names without a description: {sorted(unknown)[:6]}'
    return 'no test reaches it' if not _evidence(plan, provider) else 'described in a later step'


def explained(plan: Plan, largest: int = 12) -> list[str]:
    """The largest borrowed files held whole, and why each one could not be described."""
    sized = sorted(((_tokens(p), p) for p in plan.whole), reverse=True)[:largest]
    return [f'  {size:>7} {p.relative_to(plan.root).as_posix()}: {_why_whole(plan, p)}' for size, p in sized]


if __name__ == '__main__':
    repository = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repository / 'scripts'), str(repository)]
    raise SystemExit(main())
