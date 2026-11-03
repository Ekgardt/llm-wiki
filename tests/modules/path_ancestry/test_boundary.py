"""Shared ancestry has no dependency on either of its IO consumers."""
import ast
from pathlib import Path

import bounded_io
from llm_wiki import path_ancestry

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_predicate_owner_is_standard_library_only():
    source = (ROOT / 'scripts/llm_wiki/path_ancestry.py').read_text()
    assert not nonstdlib_imports(source)
    assert_isolated_import(ROOT, 'llm_wiki.path_ancestry', dependencies=('llm_wiki',))


def test_legacy_aliases_are_the_only_reader_implementation():
    tree = ast.parse((ROOT / 'scripts/bounded_io.py').read_text())
    definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ('_acceptable_ancestor', '_system_symlink', '_root_controlled'):
        assert name not in definitions
        assert getattr(bounded_io, name) is getattr(path_ancestry, name)
