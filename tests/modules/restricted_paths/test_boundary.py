"""Each boundary has one owner without filesystem or executor imports."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/restricted-paths-context.json')


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/restricted_paths.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'pathlib'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/restricted_paths.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'re'}
