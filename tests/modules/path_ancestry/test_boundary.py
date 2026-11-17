"""Shared ancestry has no dependency on either of its IO consumers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/path-ancestry-context.json')


def test_predicate_owner_is_standard_library_only():
    source = read_source(ROOT / 'scripts/llm_wiki/path_ancestry.py')
    assert not nonstdlib_imports(source)
    assert_isolated_import(ROOT, 'llm_wiki.path_ancestry', dependencies=('llm_wiki',))
