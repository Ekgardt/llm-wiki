"""Ledger core cannot import runtime roots, catalogs or Markdown transactions."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ledger-core-context.json')


def test_source_has_no_product_or_external_dependency():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/ledger_core.py'))


def test_fresh_import_refuses_runtime_dependencies():
    assert_isolated_import(ROOT, 'llm_wiki.ledger_core', dependencies=('llm_wiki',))
