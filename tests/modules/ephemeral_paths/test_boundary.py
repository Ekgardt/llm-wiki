"""The existing ephemeral_paths owner remains independent of product executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_owner_imports_only_standard_library():
    assert_stdlib_boundary(ROOT, 'ephemeral_paths')
    assert_isolated_import(ROOT, 'ephemeral_paths')
