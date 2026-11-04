"""A supplied-source reader imports only the existing parsing boundary."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/value-references-context.json')


def test_only_the_parser_is_a_product_dependency():
    source = read_source(ROOT / 'scripts/value_references.py')
    assert nonstdlib_imports(source) == {'python_parse'}


def test_import_cannot_reach_graph_or_repository_state():
    assert_isolated_import(ROOT, 'value_references', dependencies=('python_parse',))
