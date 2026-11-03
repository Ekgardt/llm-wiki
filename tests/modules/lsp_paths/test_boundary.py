"""Preserve the existing independent import boundary."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_source_has_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'lsp_paths')


def test_import_refuses_other_product_modules():
    assert_isolated_import(ROOT, 'lsp_paths')
