"""Runtime layers must not import the former facade or higher executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/runtime-fingerprints-context.json')


def test_owner_has_only_declared_lower_dependencies():
    source = read_source(ROOT / 'scripts/llm_wiki/runtime_fingerprints.py')
    assert nonstdlib_imports(source) <= {'llm_wiki.runtime_records', 'llm_wiki.sqlite_deadlines', 'bounded_io'} | {'windows_workspace', 'llm_wiki.runtime_environment', 'llm_wiki.runtime_contracts'}


def test_import_never_reaches_the_facade_or_executor():
    assert_isolated_import(ROOT, 'llm_wiki.runtime_fingerprints', dependencies=('llm_wiki.runtime_records', 'llm_wiki.sqlite_deadlines', 'bounded_io', 'llm_wiki.path_ancestry', 'llm_wiki', 'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment', 'llm_wiki.windows_acl', 'llm_wiki.windows_native', 'windows_workspace'))
