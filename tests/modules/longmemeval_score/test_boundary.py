"""The deterministic scorer imports data preparation, never execution."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/longmemeval-score-context.json')


def test_only_data_preparation_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/longmemeval_score.py')) == {'longmemeval_data'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'longmemeval_score', dependencies=('longmemeval_data',), source_roots=('scripts', 'benchmark'))
