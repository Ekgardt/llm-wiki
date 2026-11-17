"""Byte-prefix policy is independent of navigation and queue consumers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/utf8-text-context.json')


def test_prefix_owner_has_no_product_dependencies():
    source = read_source(ROOT / 'scripts/llm_wiki/utf8_text.py')
    assert not nonstdlib_imports(source)


def test_prefix_owner_cannot_import_navigation_or_queue():
    assert_isolated_import(ROOT, 'llm_wiki.utf8_text', dependencies=('llm_wiki',))
