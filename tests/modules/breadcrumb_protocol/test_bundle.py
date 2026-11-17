"""The existing complete-wire carrier constructs without storage effects."""
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest
from breadcrumb_protocol import BreadcrumbBundle

ROOT = Path(__file__).resolve().parents[3]


def test_bundle_field_order_and_immutable_value_semantics():
    value = BreadcrumbBundle(b'manifest', b'anchor', (b'part',), b'content')
    assert [field.name for field in fields(value)] == ['manifest', 'anchor', 'parts', 'content']
    assert [field.type for field in fields(value)] == ['bytes', 'bytes', 'tuple[bytes, ...]', 'bytes']
    assert replace(value) == value
    assert {value: 'held'}[replace(value)] == 'held'
    with pytest.raises(FrozenInstanceError):
        value.content = b'changed'
    assert BreadcrumbBundle.__module__ == 'breadcrumb_storage'


def test_constructing_values_does_not_import_storage(tmp_path):
    code = (
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'sys.modules["breadcrumb_storage"] = None; '
        'sys.modules["memory_state"] = None; '
        'from breadcrumb_protocol import BreadcrumbBundle; '
        'value = BreadcrumbBundle(b"m", b"a", (b"p",), b"c"); '
        'assert value.content == b"c"'
    )
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts')],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
