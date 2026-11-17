"""Rebuild a record catalog's offsets and digests after its source changed.

The records before and after an edit still match byte for byte, which identifies
the one record the edit landed in. An edit that crosses records is refused: where
the new boundary belongs is a reviewer's decision, not arithmetic.

Usage: python -m tests.regenerate_module_records docs/modules/<name>-records.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from itertools import takewhile
from pathlib import Path

from tests.module_records import validated_records

ROOT = Path(__file__).resolve().parents[1]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _intact(row: dict, source: bytes, shift: int) -> bool:
    start, end = max(row['start'] + shift, 0), max(row['end'] + shift, 0)
    return _digest(source[start:end]) == row['sha256']


def _intact_count(rows: Sequence[dict], source: bytes, shift: int) -> int:
    return sum(1 for _ in takewhile(lambda row: _intact(row, source, shift), rows))


def _edited_span(rows: list[dict], source: bytes, shift: int) -> tuple[int, int]:
    """An insertion exactly on a boundary leaves every record intact: the next one takes it."""
    first = min(_intact_count(rows, source, 0), len(rows) - 1)
    after = min(_intact_count(rows[::-1], source, shift), len(rows) - first - 1)
    return first, len(rows) - after


def _moved(rows: list[dict], first: int, shift: int) -> list[dict]:
    grown = {**rows[first], 'end': rows[first]['end'] + shift}
    shifted = [{**row, 'start': row['start'] + shift, 'end': row['end'] + shift} for row in rows[first + 1:]]
    return [*rows[:first], grown, *shifted]


def rebuilt(catalog: dict, source: bytes) -> dict:
    """The same records, re-measured against the source's present bytes."""
    rows = catalog['records']
    shift = len(source) - catalog['bytes']
    first, stop = _edited_span(rows, source, shift)
    if stop - first != 1:
        names = ', '.join(row['id'] for row in rows[first:stop])
        raise ValueError(f'The edit crosses records {names}; set their boundaries by hand')
    records = [{**row, 'sha256': _digest(source[row['start']:row['end']])} for row in _moved(rows, first, shift)]
    result = {**catalog, 'bytes': len(source), 'sha256': _digest(source), 'records': records}
    validated_records(result, source)
    return result


def _rendered(catalog: dict, original: str) -> bytes:
    indent = 2 if original.startswith('{\n  ') else None
    ending = '\n' if original.endswith('\n') else ''
    return (json.dumps(catalog, indent=indent, ensure_ascii=False) + ending).encode('utf-8')


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    path = parser.parse_args(argv).catalog
    original = path.read_bytes().decode('utf-8')
    catalog = json.loads(original)
    path.write_bytes(_rendered(rebuilt(catalog, (ROOT / catalog['source']).read_bytes()), original))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
