"""Each boundary has one owner without filesystem or executor imports."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_filesystem_module_no_longer_defines_owner_function():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    assert 'restricted_relative_path' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/restricted_paths.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'pathlib'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/restricted_paths.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'re'}
