"""Existing owners stay independent from orchestration and providers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_only_declared_dependencies():
    assert nonstdlib_imports((ROOT / 'scripts/answer_cost.py').read_text()) == {'answer_budget'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'answer_cost', dependencies=('answer_budget',))
