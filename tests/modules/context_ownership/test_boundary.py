"""Inventory code depends only on declared context and record inventory helpers."""
from pathlib import Path

from tests.module_boundaries import nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/context-ownership-context.json')


def test_ownership_inventory_dependencies_are_explicit():
    assert nonstdlib_imports(read_source(ROOT / 'tests/module_ownership.py')) == {
        'tests.module_context', 'tests.module_record_ownership',
    }
