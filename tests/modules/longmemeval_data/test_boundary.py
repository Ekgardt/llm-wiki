"""A report reader cannot import runtime or benchmark executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/longmemeval-data-context.json')


def test_only_stdlib_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'benchmark/longmemeval_data.py'))


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'longmemeval_data', source_roots=('scripts', 'benchmark'))
