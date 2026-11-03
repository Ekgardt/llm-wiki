"""The owner must be independent of filesystem IO and its executors."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_filesystem_module_no_longer_owns_control():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    assert 'operational_deadline_active' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_owner_does_not_import_io_or_executors():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_deadlines.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'llm_wiki.sqlite_contracts',
                       'llm_wiki.sqlite_transactions'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_deadlines.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'math', 'time', 'sqlite3'}
