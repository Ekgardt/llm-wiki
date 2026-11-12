"""Existing owners stay independent from orchestration and providers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/answer-cost-context.json')


def test_only_declared_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/answer_cost.py')) == {'answer_budget'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'answer_cost', dependencies=('answer_budget',))
