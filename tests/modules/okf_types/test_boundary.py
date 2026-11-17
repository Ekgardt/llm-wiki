"""The existing okf_types owner remains independent of product executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]

read_source = context_reader(ROOT, ROOT / 'docs/modules/okf-types-context.json')


def test_owner_imports_only_standard_library():
    assert_stdlib_boundary(ROOT, 'okf_types', read_source=read_source)
    assert_isolated_import(ROOT, 'okf_types')
