"""Import resolution depends on parsing, never graph publication or LSP startup."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/import-resolver-context.json')


def test_only_python_parser_is_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/import_resolver.py')) == {'python_parse'}
    assert_isolated_import(ROOT, 'import_resolver', dependencies=('python_parse',))
