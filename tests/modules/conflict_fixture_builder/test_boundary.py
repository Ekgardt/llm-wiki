"""The file decoder depends only on the parser and optional Parquet support."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/conflict-fixture-builder-context.json')


def test_only_declared_dependencies():
    source = read_source(ROOT / 'benchmark/build_conflict_fixture.py')
    assert nonstdlib_imports(source) == {'factconsolidation_data', 'pyarrow.parquet'}


def test_import_needs_no_product_runtime():
    assert_isolated_import(
        ROOT, 'build_conflict_fixture', dependencies=('factconsolidation_data',),
        source_roots=('benchmark', 'scripts'),
    )
