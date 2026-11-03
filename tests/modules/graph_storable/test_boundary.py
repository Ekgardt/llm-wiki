"""The existing graph_storable owner must stay independent of its consumers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_owner_has_only_standard_library_dependencies():
    assert_stdlib_boundary(ROOT, 'graph_storable')


def test_owner_import_cannot_load_runtime_consumers():
    assert_isolated_import(ROOT, 'graph_storable')
