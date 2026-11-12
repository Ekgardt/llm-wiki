"""Cross-owner aliases and serialization are integration qualification: the path-ancestry readers, the
SQLite I/O records and the PEM redaction helper keep one implementation and their pickle bytes.

The queue's and the ownership and Markdown records' aliases live in
`tests/test_module_legacy_aliases_*.py`.
"""

import ast
import base64
import json
import pickle

import bounded_io
import reliable_memory
from llm_wiki import path_ancestry, sqlite_io

from tests.module_legacy_alias_helpers import (
    ROOT,
)


def test_legacy_aliases_are_the_only_reader_implementation():
    tree = ast.parse((ROOT / 'scripts/bounded_io.py').read_text(encoding='utf-8'))
    definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ('acceptable_ancestor', 'system_symlink', 'root_controlled'):
        assert name not in definitions
        assert getattr(bounded_io, name) is getattr(path_ancestry, name)


def test_public_aliases_preserve_pickle_bytes():
    fixture = ROOT / 'tests/modules/sqlite_io/legacy-pickle.json'
    for name, encoded in json.loads(fixture.read_text(encoding='utf-8')).items():
        value = getattr(sqlite_io, name)
        assert getattr(reliable_memory, name) is value
        assert pickle.dumps(value, protocol=4) == base64.b64decode(encoded)
        assert pickle.loads(base64.b64decode(encoded)) is value


def test_public_pem_helper_keeps_identity_and_serialization():
    import lsp_security
    from llm_wiki import lsp_credentials
    helper = lsp_security.redact_private_key_blocks
    assert helper is lsp_credentials.redact_private_key_blocks
    assert helper.__module__ == 'lsp_security'
    assert pickle.loads(pickle.dumps(helper)) is helper
