"""Only the declared optional Parquet dependency may cross this data boundary."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/beam-data-context.json')


def test_only_the_declared_parquet_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/beam_data.py')) == {'pyarrow.parquet'}


def test_module_import_does_not_load_runtime_providers():
    assert_isolated_import(ROOT, 'beam_data', source_roots=('benchmark', 'scripts'))
