"""Existing owners stay independent from orchestration and providers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_only_declared_dependencies():
    assert nonstdlib_imports((ROOT / 'scripts/answer_budget.py').read_text()) == set()


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'answer_budget')
