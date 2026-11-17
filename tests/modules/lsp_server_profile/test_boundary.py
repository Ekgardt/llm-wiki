"""Profile values cannot depend on installed servers or runtime owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]

read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-server-profile-context.json')


def test_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'lsp_server_profile', read_source=read_source)


def test_import_cannot_reach_product_runtime():
    assert_isolated_import(ROOT, 'lsp_server_profile')
