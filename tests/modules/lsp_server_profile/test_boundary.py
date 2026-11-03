"""Profile values cannot depend on installed servers or runtime owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'lsp_server_profile')


def test_import_cannot_reach_product_runtime():
    assert_isolated_import(ROOT, 'lsp_server_profile')
