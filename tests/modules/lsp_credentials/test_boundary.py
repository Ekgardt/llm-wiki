"""Credential masking has one owner and no filesystem/runtime imports."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-credentials-context.json')


def test_credential_stage_import_is_isolated():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_credentials', dependencies=('llm_wiki', 'llm_wiki.lsp_text'))


def test_credential_stage_depends_only_on_text_predicate():
    source = read_source(ROOT / 'scripts/llm_wiki/lsp_credentials.py')
    assert nonstdlib_imports(source) == {'llm_wiki.lsp_text'}
