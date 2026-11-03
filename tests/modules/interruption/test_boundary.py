"""Interruption propagation cannot acquire LSP runtime dependencies."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'interruption')


def test_import_cannot_reach_product_runtime():
    assert_isolated_import(ROOT, 'interruption')
