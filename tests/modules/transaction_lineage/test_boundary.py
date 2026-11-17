"""A supplied-connection reader must not import its operational callers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/transaction-lineage-context.json')


def test_source_uses_only_standard_library():
    assert_stdlib_boundary(ROOT, 'transaction_lineage', read_source=read_source)


def test_import_does_not_load_coordinator_or_doctor():
    assert_isolated_import(ROOT, 'transaction_lineage')
