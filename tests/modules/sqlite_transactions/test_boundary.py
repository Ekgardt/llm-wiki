"""Transaction control is independent of filesystem IO and its callers."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/sqlite-transactions-context.json')


def test_owner_uses_only_standard_library():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_transactions.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc'}


def test_direct_imports_are_only_transaction_libraries():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_transactions.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'contextlib', 'sqlite3'}
