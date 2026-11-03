"""Cross-owner aliases and serialization are integration qualification."""
import ast
import base64
import json
import pickle
from pathlib import Path

import bounded_io
import memory_queue
import reliable_memory
from llm_wiki import path_ancestry, queue_artifacts, sqlite_io

ROOT = Path(__file__).resolve().parents[1]


def _definitions(path):
    tree = ast.parse((ROOT / path).read_text())
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_legacy_names_are_identical_and_have_no_second_implementation():
    names = _definitions('scripts/llm_wiki/queue_artifacts.py')
    definitions = _definitions('scripts/memory_queue.py')
    assert not names & definitions
    for name in names:
        assert getattr(memory_queue, name) is getattr(queue_artifacts, name)


def test_legacy_aliases_are_the_only_reader_implementation():
    tree = ast.parse((ROOT / 'scripts/bounded_io.py').read_text())
    definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ('_acceptable_ancestor', '_system_symlink', '_root_controlled'):
        assert name not in definitions
        assert getattr(bounded_io, name) is getattr(path_ancestry, name)


def test_public_aliases_preserve_pickle_bytes():
    fixture = ROOT / 'tests/modules/sqlite_io/legacy-pickle.json'
    for name, encoded in json.loads(fixture.read_text()).items():
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
