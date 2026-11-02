"""Value contracts must not pull filesystem/database execution into consumers."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'OperationalDatabaseContract', 'OperationalDatabaseContractError',
         'MigrationStatement', 'quote_sqlite_identifier',
         '_require_positive_int', '_require_migration_text'}


def test_durability_module_has_no_contract_definitions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    assert not NAMES.intersection(n.name for n in nodes)


def test_owner_from_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_contracts.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'dataclasses'}


def test_owner_direct_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/sqlite_contracts.py').read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in imports for a in n.names} <= {'sqlite3'}
