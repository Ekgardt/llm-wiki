"""Helpers shared by test_module_records.py and the test files importing them."""

import hashlib

import pytest

PARTS = [b'# First\n' + 'Я\n'.encode(), b'# Second\n' + '文\n'.encode()]


SOURCE = b''.join(PARTS)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _record(identifier, start, content):
    return {'id': identifier, 'label': identifier, 'start': start,
            'end': start + len(content), 'sha256': _digest(content)}


@pytest.fixture
def catalog():
    return {'schema': 'module-records/v1', 'source': 'docs/history.md',
            'bytes': len(SOURCE), 'sha256': _digest(SOURCE),
            'records': [_record('first', 0, PARTS[0]),
                        _record('second', len(PARTS[0]), PARTS[1])]}
