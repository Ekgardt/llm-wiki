"""Release digest calculation does not depend on product runtime modules."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/release-manifest-context.json')


def test_release_manifest_has_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'release_manifest', read_source=read_source)


def test_release_manifest_import_needs_no_product_runtime():
    assert_isolated_import(ROOT, 'release_manifest')
