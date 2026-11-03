"""The normalization stage cannot import redaction or repository owners."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_text_stage_uses_only_standard_library():
    assert not nonstdlib_imports((ROOT / 'scripts/llm_wiki/lsp_text.py').read_text())


def test_text_stage_import_cannot_reach_security_or_process_owners():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_text', dependencies=('llm_wiki',))


def test_security_has_no_second_normalization_implementation():
    tree = ast.parse((ROOT / 'scripts/lsp_security.py').read_text())
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not defined.intersection({'_is_control', '_collapses_to_space', '_LogScanner', '_normalize_log_text'})
