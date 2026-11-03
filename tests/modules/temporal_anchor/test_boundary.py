"""Keep existing pure computation independent from orchestration."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_only_declared_computation_dependencies():
    assert_stdlib_boundary(ROOT, 'temporal_anchor')


def test_import_does_not_load_retrieval_or_the_queue():
    assert_isolated_import(ROOT, 'temporal_anchor')
