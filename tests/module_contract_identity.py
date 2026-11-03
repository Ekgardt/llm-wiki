"""Byte identity for contract evidence; never a qualification result.

The caller must derive required paths from actual complete owner packets,
contracts and tooling. A caller-selected list alone cannot prove completeness.
Environment and successful real test execution are separate obligations.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

SCHEMA = 'dependency-artifact-identity/v1'


def _canonical_name(name: str) -> str:
    if not isinstance(name, str) or '\\' in name:
        raise ValueError('Artifact path must be a canonical relative POSIX path')
    return _relative_name(name)


def _relative_name(name: str) -> str:
    path = PurePosixPath(name)
    invalid = (path.is_absolute(), '..' in path.parts, str(path) != name, name == '.')
    if any(invalid):
        raise ValueError(f'Noncanonical artifact path: {name}')
    return name


def _artifact(root: Path, name: str) -> Path:
    path = root / _canonical_name(name)
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError(f'Artifact escapes checkout: {name}')
    if not path.is_file():
        raise ValueError(f'Artifact is not a file: {name}')
    return path


def artifact_identity(root: Path, names: set[str]) -> dict:
    """Hash complete bytes; membership and schema are part of the identity."""
    if not names:
        raise ValueError('An empty artifact set cannot identify a contract')
    files = {name: _file_identity(_artifact(root, name)) for name in sorted(names)}
    return {'schema': SCHEMA, 'files': files}


def _file_identity(path: Path) -> dict:
    content = path.read_bytes()
    return {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


def require_artifact_identity(root: Path, names: set[str], expected: dict) -> None:
    """Reject stale, missing, extra or differently encoded evidence inputs."""
    actual = artifact_identity(root, names)
    if _encoded(actual) != _encoded(expected):
        raise ValueError('Contract artifact identity changed; requalification required')


def _encoded(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
