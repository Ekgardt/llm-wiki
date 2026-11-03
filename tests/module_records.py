"""Exact byte partitions for reviewed documentation, benchmark and lock records.

This validator proves byte identity and coverage, not semantic record boundaries.
Executable source and mandatory whole-file contracts retain their normal checks.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

_CATALOG_FIELDS = {'schema', 'source', 'bytes', 'sha256', 'records'}
_RECORD_FIELDS = {'id', 'label', 'start', 'end', 'sha256'}
_IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]*\Z')


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _fields(value, expected):
    _require(isinstance(value, dict), 'Record metadata must be an object')
    _require(set(value) == expected, 'Record metadata fields changed')


def _supported_source(path):
    if path == PurePosixPath('uv.lock'):
        return True
    return path.suffix == {'docs': '.md', 'benchmark': '.json'}.get(path.parts[0])


def _source_name(value):
    _require(isinstance(value, str) and bool(value), 'Record source must be named')
    path = PurePosixPath(value)
    _require(bool(path.parts), 'Record source must name an artifact')
    _require(not path.is_absolute() and path.as_posix() == value, 'Record source must be canonical relative path')
    _require('..' not in path.parts and '\\' not in value, 'Record source escapes its path')
    _require(_supported_source(path), 'Record source class is unsupported')


def _catalog_shape(catalog):
    _fields(catalog, _CATALOG_FIELDS)
    _require(catalog['schema'] == 'module-records/v1', 'Unsupported record catalog')
    _source_name(catalog['source'])


def _source_identity(catalog, source):
    _require(type(catalog['bytes']) is int, 'Source byte length must be an integer')
    _require(catalog['bytes'] == len(source), 'Source byte length changed')
    _require(catalog['sha256'] == hashlib.sha256(source).hexdigest(), 'Source digest changed')


def _record_identity(row):
    identifier = row['id']
    _require(isinstance(identifier, str), 'Record id must be text')
    _require(_IDENTIFIER.fullmatch(identifier) is not None, 'Record id must be a stable slug')
    _require(isinstance(row['label'], str) and bool(row['label'].strip()), 'Record label must be meaningful text')


def _record_bytes(row, source, start):
    _fields(row, _RECORD_FIELDS)
    _record_identity(row)
    _require(type(row['start']) is int and type(row['end']) is int, 'Record bounds must be integer bytes')
    _require(row['start'] == start, 'Record partition has a gap or overlap')
    _require(start < row['end'] <= len(source), 'Record bounds must contain nonempty source bytes')
    content = source[start:row['end']]
    content.decode('utf-8', errors='strict')
    _require(row['sha256'] == hashlib.sha256(content).hexdigest(), 'Record digest changed')
    return content


def validated_records(catalog: dict, source: bytes) -> dict[str, bytes]:
    """Validate all source bytes, including records a later packet may not select."""
    _catalog_shape(catalog)
    _source_identity(catalog, source)
    rows = catalog['records']
    _require(isinstance(rows, list) and bool(rows), 'Catalog must partition a nonempty source')
    parts = {}
    end = 0
    for row in rows:
        content = _record_bytes(row, source, end)
        _require(row['id'] not in parts, 'Duplicate record id')
        parts[row['id']] = content
        end = row['end']
    _require(end == len(source), 'Record partition omits source tail')
    _require(b''.join(parts.values()) == source, 'Record partition changed source bytes')
    return parts


def _selected_ids(selected, parts):
    _require(isinstance(selected, list), 'Record selection must be a list')
    _require(all(isinstance(item, str) for item in selected), 'Selected record ids must be text')
    _require(len(set(selected)) == len(selected), 'Duplicate selected record id')
    _require(set(selected) <= parts.keys(), 'Unknown selected record id')


def record_entries(catalog: dict, source: bytes, selected: list[str]) -> dict[str, bytes]:
    parts = validated_records(catalog, source)
    _selected_ids(selected, parts)
    prefix = '@record/' + catalog['source'] + '#'
    return {prefix + identifier: parts[identifier] for identifier in selected}


def _unique_object(pairs):
    result = dict(pairs)
    _require(len(result) == len(pairs), 'Duplicate record catalog field')
    return result


def _invalid_constant(value):
    raise ValueError(f'Nonstandard JSON constant: {value}')


def read_catalog(data: bytes) -> dict:
    catalog = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    _catalog_shape(catalog)
    return catalog
