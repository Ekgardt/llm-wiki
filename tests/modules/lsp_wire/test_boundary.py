"""The wire codec cannot depend on transport or process owners."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_wire_has_only_its_declared_io_dependency():
    source = (ROOT / 'scripts/llm_wiki/lsp_wire.py').read_text()
    assert nonstdlib_imports(source) == {'bounded_io'}


def test_wire_import_cannot_start_transport_or_process_owners():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_wire', dependencies=(
        'llm_wiki', 'bounded_io', 'llm_wiki.path_ancestry'))


def test_transport_does_not_define_a_second_codec():
    tree = ast.parse((ROOT / 'scripts/lsp_protocol.py').read_text())
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not defined.intersection({'encode_frame', 'json_depth', 'JsonRpcFrameReader',
                                     'ProtocolViolation', 'FrameTooLarge'})
