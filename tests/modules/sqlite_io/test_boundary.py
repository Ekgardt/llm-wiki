"""One implementation, exact legacy aliases and no upward import."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/sqlite-io-context.json')


def test_import_only_reaches_declared_lower_owners():
    assert_isolated_import(ROOT, 'llm_wiki.sqlite_io', dependencies=(
        'llm_wiki', 'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_metadata', 'llm_wiki.windows_acl', 'llm_wiki.windows_native',
        'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_pragmas',
        'llm_wiki.sqlite_deadlines', 'llm_wiki.path_ancestry',
    ))


def test_lazy_and_eager_imports_name_only_lower_owners():
    source = read_source(ROOT / 'scripts/llm_wiki/sqlite_io.py')
    assert nonstdlib_imports(source) <= {
        'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_metadata', 'llm_wiki.sqlite_contracts',
        'llm_wiki.sqlite_pragmas', 'llm_wiki.sqlite_deadlines',
        'llm_wiki.path_ancestry',
    }
