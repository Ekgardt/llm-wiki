"""Bring every manifest's derived descriptions back in line with the real code."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.module_context import context_files
from tests.module_interface import packet_accesses
from tests.module_qualification import contract_digest, contract_text


def _refreshed_entry(root: Path, entry: dict, accessed: dict[Path, set[str]]) -> dict:
    provider = root / entry['provider']
    names = sorted(accessed.get(provider, set()))
    return {**entry, 'names': names, 'contract_sha256': contract_digest(contract_text(root, provider, set(names)))}


def refresh_manifest(root: Path, path: Path) -> bool:
    manifest = json.loads(path.read_bytes())
    entries = manifest.get('qualified_dependencies')
    if not entries:
        return False
    accessed = packet_accesses(root, context_files(root, manifest)).accessed
    updated = [_refreshed_entry(root, entry, accessed) for entry in entries]
    changed = updated != entries
    if changed:
        path.write_text(json.dumps({**manifest, 'qualified_dependencies': updated}, indent=2) + '\n', encoding='utf-8')
    return changed


def main() -> int:
    root = Path.cwd().resolve()
    changed = [path.name for path in sorted((root / 'docs/modules').glob('*-context.json')) if refresh_manifest(root, path)]
    print(json.dumps({'refreshed': changed}, indent=2))
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    raise SystemExit(main())
