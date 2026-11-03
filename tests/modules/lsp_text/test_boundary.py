"""The normalization stage cannot import redaction or repository owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-text-context.json')


def test_text_stage_uses_only_standard_library():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/lsp_text.py'))


def test_text_stage_import_cannot_reach_security_or_process_owners():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_text', dependencies=('llm_wiki',))
