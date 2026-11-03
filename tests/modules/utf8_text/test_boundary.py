"""Byte-prefix policy is independent of navigation and queue consumers."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_prefix_owner_has_no_product_dependencies():
    source = (ROOT / 'scripts/llm_wiki/utf8_text.py').read_text()
    assert not nonstdlib_imports(source)


def test_prefix_owner_cannot_import_navigation_or_queue():
    assert_isolated_import(ROOT, 'llm_wiki.utf8_text', dependencies=('llm_wiki',))


def test_renderer_has_no_second_prefix_implementation():
    tree = ast.parse((ROOT / 'scripts/code_navigation_renderer.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert '_utf8_prefix' not in names
