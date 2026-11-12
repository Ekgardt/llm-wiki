"""Reader leases do not import graph selection or persistent state."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/evidence-reader-cache-context.json')


def test_source_uses_only_standard_library():
    assert_stdlib_boundary(ROOT, 'evidence_reader_cache', read_source=read_source)


def test_import_does_not_load_product_consumers():
    assert_isolated_import(ROOT, 'evidence_reader_cache')
