"""Verify that a universal advisory report covers every locked registry version."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def _pair(record: dict) -> tuple[str, str]:
    name, version = record.get('name'), record.get('version')
    if not isinstance(name, str) or not name:
        raise ValueError('Dependency name must be a nonempty string')
    if not isinstance(version, str) or not version:
        raise ValueError(f'Dependency version missing: {name}')
    return name, version


def _locked_pair(package: dict) -> tuple[str, str] | None:
    source = package['source']
    if source in ({'virtual': '.'}, {'editable': '.'}):
        return None
    if set(source) != {'registry'}:
        raise ValueError(f'Unsupported locked source for {package.get("name")}')
    return _pair(package)


def locked_pairs(lock: dict) -> set[tuple[str, str]]:
    pairs = {_locked_pair(package) for package in lock['package']}
    pairs.discard(None)
    if not pairs:
        raise ValueError('No registry dependencies to audit')
    return pairs


def _audited_pair(record: dict) -> tuple[str, str]:
    if 'skip_reason' in record:
        raise ValueError(f'Audit skipped {record.get("name")}')
    if not isinstance(record.get('vulns'), list) or record['vulns']:
        raise ValueError(f'Missing advisory result or known vulnerabilities in {record.get("name")}')
    return _pair(record)


def verify_coverage(lock: dict, report: dict) -> int:
    expected = locked_pairs(lock)
    actual = {_audited_pair(record) for record in report['dependencies']}
    if actual != expected:
        raise ValueError(f'Audit coverage mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}')
    return len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lock', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args(argv)
    lock = tomllib.loads(args.lock.read_text(encoding='utf-8'))
    report = json.loads(args.report.read_text(encoding='utf-8'))
    count = verify_coverage(lock, report)
    print(json.dumps({'status': 'covered', 'locked_versions': count}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
