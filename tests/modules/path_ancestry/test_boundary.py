"""Shared ancestry has no dependency on either of its IO consumers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_predicate_owner_is_standard_library_only():
    source = (ROOT / 'scripts/llm_wiki/path_ancestry.py').read_text()
    assert not nonstdlib_imports(source)
    assert_isolated_import(ROOT, 'llm_wiki.path_ancestry', dependencies=('llm_wiki',))
