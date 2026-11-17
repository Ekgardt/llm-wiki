"""The wire codec cannot depend on transport or process owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-wire-context.json')


def test_wire_has_only_its_declared_io_dependency():
    source = read_source(ROOT / 'scripts/llm_wiki/lsp_wire.py')
    assert nonstdlib_imports(source) == {'bounded_io'}


def test_wire_import_cannot_start_transport_or_process_owners():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_wire', dependencies=(
        'llm_wiki', 'bounded_io', 'llm_wiki.path_ancestry'))
