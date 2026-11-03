"""Credential masking has one owner and no filesystem/runtime imports."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_credential_stage_import_is_isolated():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_credentials', dependencies=('llm_wiki', 'llm_wiki.lsp_text'))


def test_credential_stage_depends_only_on_text_predicate():
    source = (ROOT / 'scripts/llm_wiki/lsp_credentials.py').read_text()
    assert nonstdlib_imports(source) == {'llm_wiki.lsp_text'}


def test_security_has_no_duplicate_credential_implementation():
    tree = ast.parse((ROOT / 'scripts/lsp_security.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'_quoted_value_end', '_line_value_end',
        '_assignment_value_end', '_redact_assignments', '_standalone_replacement',
        '_redact_standalone_secrets', '_scheme_character', '_scheme_start',
        '_is_scheme', '_authority_terminator', '_authority_end', '_userinfo_span',
        '_redact_url_userinfo', 'redact_private_key_blocks'})
