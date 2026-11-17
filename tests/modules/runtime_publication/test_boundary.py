"""Runtime layers must not import the former facade or higher executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/runtime-publication-context.json')


def test_owner_has_only_declared_lower_dependencies():
    source = read_source(ROOT / 'scripts/llm_wiki/runtime_publication.py')
    assert nonstdlib_imports(source) <= {'llm_wiki.runtime_sync', 'llm_wiki.formats.canonical', 'windows_workspace', 'llm_wiki.windows_acl', 'llm_wiki.runtime_records', 'llm_wiki.sqlite_contracts', 'llm_wiki.runtime_environment', 'llm_wiki.runtime_contracts'}


def test_import_never_reaches_the_facade_or_executor():
    assert_isolated_import(ROOT, 'llm_wiki.runtime_publication', dependencies=('llm_wiki.runtime_metadata', 'bounded_io', 'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment', 'llm_wiki.runtime_records', 'llm_wiki.runtime_sync', 'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_deadlines', 'llm_wiki.windows_acl', 'llm_wiki.windows_native', 'windows_workspace'))
