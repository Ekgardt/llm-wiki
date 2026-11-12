"""The owner must be independent of filesystem IO and its executors."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/sqlite-migrations-context.json')


def test_owner_does_not_import_io_or_executors():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_migrations.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'llm_wiki.sqlite_contracts',
                       'llm_wiki.sqlite_transactions'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_migrations.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'sqlite3'}
