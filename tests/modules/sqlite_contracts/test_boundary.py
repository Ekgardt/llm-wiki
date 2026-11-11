"""Value contracts must not pull filesystem/database execution into consumers."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/sqlite-contracts-context.json')


def test_owner_from_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_contracts.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'dataclasses'}


def test_owner_direct_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/sqlite_contracts.py'))
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in imports for a in n.names} <= {'sqlite3'}
