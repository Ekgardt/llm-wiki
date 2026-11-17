"""Paired statistics import the real scorer, never worker execution."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/consolidation-score-context.json')


def test_only_scoring_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/consolidation_score.py')) == {'longmemeval_score'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'consolidation_score', dependencies=('longmemeval_score', 'longmemeval_data'), source_roots=('scripts', 'benchmark'))
