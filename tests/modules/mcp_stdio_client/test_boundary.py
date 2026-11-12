"""The benchmark wire adapter has no project or third-party imports."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/mcp-stdio-client-context.json')


def test_imports_only_standard_library():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/mcp_stdio_client.py')) == set()
    assert_isolated_import(ROOT, 'mcp_stdio_client', source_roots=('scripts', 'benchmark'))
