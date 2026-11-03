"""Coordinate conversion depends only on normalized value contracts."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_only_code_value_contract_dependency():
    source = (ROOT / 'scripts/lsp_positions.py').read_text()
    assert nonstdlib_imports(source) == {'code_intelligence'}


def test_import_cannot_reach_lsp_transport_or_processes():
    assert_isolated_import(ROOT, 'lsp_positions', dependencies=('code_intelligence',))
