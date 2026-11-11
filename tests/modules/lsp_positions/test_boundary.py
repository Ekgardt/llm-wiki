"""Coordinate conversion depends only on normalized value contracts."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-positions-context.json')


def test_only_code_value_contract_dependency():
    source = read_source(ROOT / 'scripts/lsp_positions.py')
    assert nonstdlib_imports(source) == {'code_intelligence'}


def test_import_cannot_reach_lsp_transport_or_processes():
    assert_isolated_import(ROOT, 'lsp_positions', dependencies=('code_intelligence',))
