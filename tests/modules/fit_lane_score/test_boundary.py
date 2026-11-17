"""Existing owners stay independent from orchestration and providers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/fit-lane-score-context.json')


def test_only_declared_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/fit_lane_score.py')) == {'lane_score', 'numpy'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'fit_lane_score', dependencies=('lane_score',), source_roots=('scripts', 'benchmark'))
