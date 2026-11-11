"""Preserve the existing independent import boundary."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]

read_source = context_reader(ROOT, ROOT / 'docs/modules/iso-time-context.json')


def test_source_has_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'iso_time', read_source=read_source)


def test_import_refuses_other_product_modules():
    assert_isolated_import(ROOT, 'iso_time')
