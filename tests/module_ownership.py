"""Read-only file ownership inventory; partial coverage is never completion."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _require_repository_root(root: Path) -> None:
    result = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', '--show-prefix'],
        check=True, stdout=subprocess.PIPE,
    )
    assert not result.stdout.strip(b'\r\n'), 'Ownership root must be the repository root'


def tracked_files(root: Path) -> set[str]:
    _require_repository_root(root)
    result = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '--cached', '-z'],
        check=True, stdout=subprocess.PIPE,
    )
    return {os.fsdecode(name) for name in result.stdout.split(b'\0') if name}


def _relative_names(root: Path, paths: set[Path]) -> set[str]:
    return {p.relative_to(root).as_posix() for p in paths}


def _claimed_files(root: Path, manifest: Path, tracked: set[str]) -> set[str]:
    from tests.module_context import context_files

    data = json.loads(manifest.read_bytes())
    declaration = data.get('ownership')
    assert isinstance(declaration, dict), f'Missing ownership declaration: {manifest.name}'
    context = context_files(root, data)
    owned = context_files(root, declaration)
    assert manifest.resolve() in owned, f'Manifest must own itself: {manifest.name}'
    assert owned <= context, f'Ownership outside context: {sorted(_relative_names(root, owned - context))}'
    names = _relative_names(root, owned)
    assert names <= tracked, f'Untracked ownership: {sorted(names - tracked)}'
    return names


def _register_owner(ownership: dict[str, str], owner: str, names: set[str]) -> None:
    for name in sorted(names):
        assert name not in ownership, f'Duplicate ownership: {name} ({ownership.get(name)}, {owner})'
        ownership[name] = owner


def _registry(root: Path, tracked: set[str]) -> dict[str, str]:
    ownership: dict[str, str] = {}
    for manifest in sorted((root / 'docs/modules').glob('*-context.json')):
        names = _claimed_files(root, manifest, tracked)
        _register_owner(ownership, manifest.name.removesuffix('-context.json'), names)
    return dict(sorted(ownership.items()))


def _record_contexts(root):
    from tests.module_context import context_files

    for manifest in sorted((root / 'docs/modules').glob('*-context.json')):
        data = json.loads(manifest.read_bytes())
        names = _relative_names(root, context_files(root, data))
        yield manifest.name.removesuffix('-context.json'), data, names


VENDORED_VOCABULARIES = 'tests/data/tiktoken'
PINNED_VOCABULARIES = {
    'o200k_base': ('https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken',
                   '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'),
}


def _verified_vocabulary(folder: Path, name: str) -> str:
    url, digest = PINNED_VOCABULARIES[name]
    path = folder / hashlib.sha1(url.encode()).hexdigest()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f'Vendored {name} differs from its pin'
    return path.name


def _tracked_vocabulary_names(tracked: set[str]) -> set[str]:
    prefix = f'{VENDORED_VOCABULARIES}/'
    return {name.removeprefix(prefix) for name in tracked if name.startswith(prefix)}


def _pinned_vendored_data(root: Path, tracked: set[str], ownership: dict[str, str]) -> set[str]:
    present = _tracked_vocabulary_names(tracked)
    if not present:
        return set()
    pinned = {_verified_vocabulary(root / VENDORED_VOCABULARIES, name) for name in PINNED_VOCABULARIES}
    assert present == pinned, f'Unpinned vendored vocabulary: {sorted(present - pinned)}'
    vendored = {f'{VENDORED_VOCABULARIES}/{name}' for name in present}
    assert vendored.isdisjoint(ownership), f'Vendored data also claimed: {sorted(vendored & ownership.keys())}'
    return vendored


def ownership_report(root: Path, *, tracked: set[str] | None = None) -> dict:
    root = root.resolve()
    if tracked is None:
        tracked = tracked_files(root)
    ownership = _registry(root, tracked)
    from tests.module_record_ownership import record_ownership_report

    records = record_ownership_report(root, tracked, ownership, _record_contexts(root))
    vendored = _pinned_vendored_data(root, tracked, ownership)
    covered = ownership.keys() | set(records['fully_recorded']) | vendored
    unassigned = sorted(tracked - covered)
    return {
        'schema_version': 'module-ownership/v2',
        'complete': not unassigned,
        'tracked_count': len(tracked),
        'owned_count': len(covered - vendored),
        'pinned_count': len(vendored),
        'unassigned_count': len(unassigned),
        'ownership': ownership,
        'unassigned': unassigned,
        'pinned_vendored_data': sorted(vendored),
        **records,
    }


def main(argv=None) -> int:
    # Match the repository's pytest pythonpath for standalone maintenance use.
    repository = Path(__file__).resolve().parents[1]
    for entry in (str(repository / 'scripts'), str(repository)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--check-complete', action='store_true')
    args = parser.parse_args(argv)
    report = ownership_report(args.root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(args.check_complete and not report['complete'])


if __name__ == '__main__':
    raise SystemExit(main())
