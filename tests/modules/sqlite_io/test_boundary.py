"""One implementation, exact legacy aliases and no upward import."""
import ast
import base64
import json
import pickle
from pathlib import Path

import reliable_memory
from llm_wiki import sqlite_io

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_facade_has_no_function_bodies():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    assert not any(isinstance(n, ast.FunctionDef) for n in tree.body)


def test_public_aliases_preserve_pickle_bytes():
    fixture = Path(__file__).with_name('legacy-pickle.json')
    for name, encoded in json.loads(fixture.read_text()).items():
        value = getattr(sqlite_io, name)
        assert getattr(reliable_memory, name) is value
        assert pickle.dumps(value, protocol=4) == base64.b64decode(encoded)
        assert pickle.loads(base64.b64decode(encoded)) is value


def test_import_only_reaches_declared_lower_owners():
    assert_isolated_import(ROOT, 'llm_wiki.sqlite_io', dependencies=(
        'llm_wiki', 'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_records', 'llm_wiki.windows_acl', 'llm_wiki.windows_native',
        'windows_workspace', 'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_pragmas',
        'llm_wiki.sqlite_deadlines', 'llm_wiki.path_ancestry',
    ))


def test_lazy_and_eager_imports_name_only_lower_owners():
    source = (ROOT / 'scripts/llm_wiki/sqlite_io.py').read_text()
    assert nonstdlib_imports(source) <= {
        'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_records', 'llm_wiki.sqlite_contracts',
        'llm_wiki.sqlite_pragmas', 'llm_wiki.sqlite_deadlines',
        'llm_wiki.path_ancestry',
    }
