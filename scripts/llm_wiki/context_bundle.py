"""Reversible readable maintenance bundles with exact duplicate file references."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath

_HEADER = ('CONTEXT exact-duplicates/v1\n'
           'SAME records reuse the exact bytes of an earlier file; every path remains included.\n')


def _require_name(name: object) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError('Bundle path must be nonempty text')
    _require_relative_name(PurePosixPath(name), name)


def _require_relative_name(path: PurePosixPath, name: str) -> None:
    if not path.parts or path.is_absolute():
        raise ValueError('Bundle path must be relative')
    if '..' in path.parts or path.as_posix() != name:
        raise ValueError('Bundle path must be canonical')


def _metadata(kind: str, name: str, value: object) -> str:
    return kind + ' ' + json.dumps([name, value], ensure_ascii=False, separators=(',', ':')) + '\n'


def _literal_record(name: str, content: bytes) -> str:
    return _metadata('FILE', name, len(content)) + content.decode('utf-8') + '\n'


def _file_record(name: str, content: bytes, seen: dict[bytes, str]) -> str:
    _require_name(name)
    literal = _literal_record(name, content)
    previous = seen.setdefault(content, name)
    if previous == name:
        return literal
    reference = _metadata('SAME', name, previous)
    if len(reference.encode('utf-8')) < len(literal.encode('utf-8')):
        return reference
    return literal


def pack_files(files: Mapping[str, bytes]) -> str:
    """Emit every name, reusing only equal byte strings already in this bundle."""
    seen: dict[bytes, str] = {}
    return _HEADER + ''.join(_file_record(name, files[name], seen) for name in sorted(files))


def _header_fields(line: bytes) -> tuple[bytes, str, object]:
    kind, encoded = line.split(b' ', 1)
    fields = json.loads(encoded)
    if not isinstance(fields, list) or len(fields) != 2:
        raise ValueError('Bundle header must contain a path and value')
    _require_name(fields[0])
    return kind, fields[0], fields[1]


def _read_header(data: bytes, position: int) -> tuple[bytes, str, object, int]:
    end = data.find(b'\n', position)
    if end < 0:
        raise ValueError('Incomplete bundle header')
    kind, name, value = _header_fields(data[position:end])
    return kind, name, value, end + 1


def _require_body_end(data: bytes, end: int) -> None:
    if end >= len(data) or data[end:end + 1] != b'\n':
        raise ValueError('Incomplete or invalid bundle body framing')


def _literal_body(length: object, data: bytes, position: int) -> tuple[bytes, int]:
    if type(length) is not int or length < 0:
        raise ValueError('Bundle byte length must be a nonnegative integer')
    end = position + length
    _require_body_end(data, end)
    content = data[position:end]
    content.decode('utf-8')
    return content, end + 1


def _referenced_body(reference: object, files: dict[str, bytes]) -> bytes:
    if not isinstance(reference, str) or reference not in files:
        raise ValueError('Bundle reference must name an earlier file')
    return files[reference]


def _record_body(kind, value, data, position, files) -> tuple[bytes, int]:
    if kind == b'FILE':
        return _literal_body(value, data, position)
    if kind == b'SAME':
        return _referenced_body(value, files), position
    raise ValueError('Unknown bundle record kind')


def _read_record(data: bytes, position: int, files: dict[str, bytes]) -> int:
    kind, name, value, position = _read_header(data, position)
    if name in files:
        raise ValueError('Duplicate bundle path')
    files[name], position = _record_body(kind, value, data, position, files)
    return position


def unpack_files(bundle: str) -> dict[str, bytes]:
    """Restore all paths and exact bytes in memory; never write to the filesystem."""
    if not bundle.startswith(_HEADER):
        raise ValueError('Unknown bundle version or header')
    data = bundle.encode('utf-8')
    position = len(_HEADER.encode('utf-8'))
    files: dict[str, bytes] = {}
    while position < len(data):
        position = _read_record(data, position, files)
    return files
