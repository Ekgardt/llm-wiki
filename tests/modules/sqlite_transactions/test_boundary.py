"""Transaction control is independent of filesystem IO and its callers."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_filesystem_module_no_longer_defines_transaction_control():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    assert 'begin_immediate' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_owner_uses_only_standard_library():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_transactions.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc'}


def test_direct_imports_are_only_transaction_libraries():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_transactions.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'contextlib', 'sqlite3'}
