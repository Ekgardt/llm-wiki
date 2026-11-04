"""Actual local interpreter/dependency bytes, never a cached qualification permit."""
from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

SCHEMA = 'dependency-environment-identity/v1'


def _derived(path: Path) -> bool:
    return '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}


def _file(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    with resolved.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if _stat_identity(before) != _stat_identity(resolved.stat()):
        raise ValueError(f'Environment file changed while reading: {path}')
    return {'target': str(resolved), 'bytes': before.st_size, 'sha256': digest}


def _stat_identity(stat) -> tuple:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _directories(parent: Path, names: list[str], seen: set, links: dict) -> list[str]:
    retained = [name for name in sorted(names) if name != '__pycache__']
    return [name for name in retained if _visit_directory(parent / name, seen, links)]


def _visit_directory(path: Path, seen: set, links: dict) -> bool:
    target = path.resolve(strict=True)
    links[str(path)] = str(target)
    if target in seen:
        return False
    seen.add(target)
    return True


def _walk_error(error: OSError) -> None:
    raise error


def _tree(root: Path) -> dict:
    root = root.resolve(strict=True)
    files = {}
    seen, links = {root}, {}
    for parent, directories, names in os.walk(root, onerror=_walk_error, followlinks=True):
        directories[:] = _directories(Path(parent), directories, seen, links)
        files.update(_tree_entries(root, Path(parent), names))
    if not files:
        raise ValueError(f'Empty interpreter/environment tree: {root}')
    return {'root': str(root), 'files': files, 'directories': links}


def _tree_entries(root: Path, parent: Path, names: list[str]) -> dict:
    paths = [parent / name for name in names]
    return {path.relative_to(root).as_posix(): _file(path)
            for path in paths if not _derived(path)}


def _distribution(dist) -> dict:
    paths = _record_paths(dist)
    if not paths or not dist.metadata['Name']:
        raise ValueError('An installed distribution has no identifiable file inventory')
    _require_not_editable(dist)
    files = {str(path): _file(Path(dist.locate_file(path)))
             for path in paths if not _derived(Path(path))}
    return {'name': dist.metadata['Name'], 'version': dist.version, 'files': files}


def _record_paths(dist) -> list[str]:
    # Distribution.files silently filters missing files on Python 3.14.
    record = dist.read_text('RECORD')
    if not record:
        raise ValueError('An installed distribution has no complete RECORD inventory')
    rows = list(csv.reader(record.splitlines()))
    if not all(_valid_record_row(row) for row in rows):
        raise ValueError('Malformed installed distribution inventory')
    return [row[0] for row in rows]


def _valid_record_row(row: list[str]) -> bool:
    return len(row) == 3 and bool(row[0])


def _require_not_editable(dist) -> None:
    direct = json.loads(dist.read_text('direct_url.json') or '{}')
    if direct.get('dir_info', {}).get('editable'):
        raise ValueError('Editable dependency sources need an explicit complete inventory')


def environment_identity() -> dict:
    """Hash actual installations and RECORD-listed files, not recorded hash claims.

    OS libraries outside these installations are not a container/sandbox identity.
    Derived bytecode is excluded because qualification uses an empty cache prefix.
    """
    roots = sorted({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()})
    distributions = [_distribution(dist) for dist in importlib.metadata.distributions()]
    return {'schema': SCHEMA, 'python': sys.version, 'platform': platform.platform(),
            'executable': _file(Path(sys.executable)),
            'installations': [_tree(root) for root in roots],
            'distributions': sorted(distributions, key=lambda item: (item['name'], item['version']))}
