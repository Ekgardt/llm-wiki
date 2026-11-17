"""Current positions depend on parsing, never graph publication or LSP startup."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/fresh-positions-context.json')


def test_only_python_parser_is_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/fresh_positions.py')) == {'python_parse'}
    assert_isolated_import(ROOT, 'fresh_positions', dependencies=('python_parse',))
